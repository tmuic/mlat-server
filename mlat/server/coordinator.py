# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-server: a Mode S multilateration server
# Copyright (C) 2015  Oliver Jowett <oliver@mutability.co.uk>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Top level glue that knows about all receivers and moves data between
the various sub-objects that make up the server.
"""

import signal
import asyncio
import json
import logging
from contextlib import closing

from mlat import geodesy
from mlat.server import tracker, clocksync, clocktrack, mlattrack, util

glogger = logging.getLogger("coordinator")


class Receiver(object):
    """Represents a particular connected receiver and the associated
    connection that manages it."""

    def __init__(self, user, connection, clock, position_llh, privacy, connection_info):
        self.user = user
        self.connection = connection
        self.clock = clock
        self.position_llh = position_llh
        self.position = geodesy.llh2ecef(position_llh)
        self.privacy = privacy
        self.connection_info = connection_info
        self.dead = False

        self.sync_count = 0
        self.last_rate_report = None
        self.tracking = set()
        self.sync_interest = set()
        self.mlat_interest = set()
        self.requested = set()

        self.distance = {}

    def update_interest_sets(self, new_sync, new_mlat):
        for added in new_sync.difference(self.sync_interest):
            added.sync_interest.add(self)

        for removed in self.sync_interest.difference(new_sync):
            removed.sync_interest.discard(self)

        for added in new_mlat.difference(self.mlat_interest):
            added.mlat_interest.add(self)

        for removed in self.mlat_interest.difference(new_mlat):
            removed.mlat_interest.discard(self)

        self.sync_interest = new_sync
        self.mlat_interest = new_mlat

    def refresh_traffic_requests(self):
        self.requested = {x for x in self.tracking if x.interesting}
        self.connection.request_traffic(self, {x.icao for x in self.requested})

    def __lt__(self, other):
        return id(self) < id(other)

    def __str__(self):
        return self.user

    def __repr__(self):
        return 'Receiver({0!r},{1!r})@{2}'.format(self.user,
                                                  self.connection,
                                                  id(self))


class Coordinator(object):
    """Master coordinator. Receives all messages from receivers and dispatches
    them to clock sync / multilateration / tracking as needed."""

    def __init__(self, authenticator=None, pseudorange_filename=None):
        """If authenticator is not None, it should be a callable that takes two arguments:
        the newly created Receiver, plus the 'auth' argument provided by the connection.
        The authenticator may modify the receiver if needed. The authenticator should either
        return silently on success, or raise an exception (propagated to the caller) on
        failure.
        """

        self.receivers = {}    # keyed by username
        self.sighup_handlers = []
        self.authenticator = authenticator
        self.tracker = tracker.Tracker()
        self.clock_tracker = clocktrack.ClockTracker()
        self.mlat_tracker = mlattrack.MlatTracker(self, pseudorange_filename=pseudorange_filename)
        self.output_handlers = [self.forward_results]

    def start(self):
        self._write_state_task = asyncio.async(self.write_state())
        return util.completed_future

    def add_output_handler(self, handler):
        self.output_handlers.append(handler)

    def remove_output_handler(self, handler):
        self.output_handlers.remove(handler)

    # it's a pity that asyncio's add_signal_handler doesn't let you have
    # multiple handlers per signal. so wire up a multiple-handler here.
    def add_sighup_handler(self, handler):
        if not self.sighup_handlers:
            asyncio.get_event_loop().add_signal_handler(signal.SIGHUP, self.sighup)
        self.sighup_handlers.append(handler)

    def remove_sighup_handler(self, handler):
        self.sighup_handlers.remove(handler)
        if not self.sighup_handlers:
            asyncio.get_event_loop().remove_signal_handler(signal.SIGHUP)

    def sighup(self):
        for handler in self.sighup_handlers[:]:
            handler()

    @asyncio.coroutine
    def write_state(self):
        while True:
            yield from asyncio.sleep(30.0)

            sync = {}
            locations = {}

            for r in self.receivers.values():
                sync[r.user] = {
                    'peers': self.clock_tracker.dump_receiver_state(r)
                }
                locations[r.user] = {
                    'lat': r.position_llh[0],
                    'lon': r.position_llh[1],
                    'alt': r.position_llh[2],
                    'privacy': r.privacy,
                    'connection': r.connection_info
                }

            with closing(open('sync.json', 'w')) as f:
                json.dump(sync, fp=f, indent=True)

            with closing(open('locations.json', 'w')) as f:
                json.dump(locations, fp=f, indent=True)

    def close(self):
        self._write_state_task.cancel()

    @asyncio.coroutine
    def wait_closed(self):
        util.safe_wait([self._write_state_task])

    def new_receiver(self, connection, user, auth, position_llh, clock_type, privacy, connection_info):
        """Assigns a new receiver ID for a given user.
        Returns the new receiver ID.

        May raise ValueError to disallow this receiver."""

        if user in self.receivers:
            raise ValueError('User {user} is already connected'.format(user=user))

        clock = clocksync.make_clock(clock_type)
        receiver = Receiver(user, connection, clock,
                            position_llh=position_llh,
                            privacy=privacy,
                            connection_info=connection_info)

        if self.authenticator is not None:
            self.authenticator(receiver, auth)  # may raise ValueError if authentication fails

        # compute inter-station distances
        receiver.distance[receiver] = 0
        for other_receiver in self.receivers.values():
            distance = geodesy.ecef_distance(receiver.position, other_receiver.position)
            receiver.distance[other_receiver] = distance
            other_receiver.distance[receiver] = distance

        self.receivers[receiver.user] = receiver  # authenticator might update user
        return receiver

    def receiver_disconnect(self, receiver):
        """Notes that the given receiver has disconnected."""

        receiver.dead = True
        if self.receivers.get(receiver.user) is receiver:
            self.tracker.remove_all(receiver)
            self.clock_tracker.receiver_disconnect(receiver)
            del self.receivers[receiver.user]

            # clean up old distance entries
            for other_receiver in self.receivers.values():
                other_receiver.distance.pop(receiver, None)

    def receiver_sync(self, receiver,
                      even_time, odd_time, even_message, odd_message):
        """Receive a DF17 message pair for clock synchronization."""
        self.clock_tracker.receiver_sync(receiver,
                                         even_time, odd_time,
                                         even_message, odd_message)

    def receiver_mlat(self, receiver, timestamp, message, utc):
        """Receive a message for multilateration."""
        self.mlat_tracker.receiver_mlat(receiver,
                                        timestamp,
                                        message,
                                        utc)

    def receiver_tracking_add(self, receiver, icao_set):
        """Update a receiver's tracking set by adding some aircraft."""
        self.tracker.add(receiver, icao_set)
        if receiver.last_rate_report is None:
            # not receiving rate reports for this receiver
            self.tracker.update_interest(receiver)

    def receiver_tracking_remove(self, receiver, icao_set):
        """Update a receiver's tracking set by removing some aircraft."""
        self.tracker.remove(receiver, icao_set)
        if receiver.last_rate_report is None:
            # not receiving rate reports for this receiver
            self.tracker.update_interest(receiver)

    def receiver_clock_reset(self, receiver):
        """Reset current clock synchronization for a receiver."""
        self.clock_tracker.receiver_clock_reset(receiver)

    def receiver_rate_report(self, receiver, report):
        """Process an ADS-B position rate report for a receiver."""
        receiver.last_rate_report = report
        self.tracker.update_interest(receiver)

    def forward_results(self, receive_timestamp, address, ecef, ecef_cov, receivers, distinct, kalman_state):
        for receiver in receivers:
            try:
                receiver.connection.report_mlat_position(receiver,
                                                         receive_timestamp, address,
                                                         ecef, ecef_cov, receivers, distinct)
            except Exception:
                glogger.exception("Failed to forward result to receiver {r}".format(r=receiver.user))
                # eat the exception so it doesn't break our caller