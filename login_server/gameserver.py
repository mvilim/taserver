#!/usr/bin/env python3
#
# Copyright (C) 2018  Maurice van der Pot <griffon26@kfk4ever.com>
#
# This file is part of taserver
#
# taserver is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# taserver is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with taserver.  If not, see <http://www.gnu.org/licenses/>.
#

import gevent.monkey
gevent.monkey.patch_all()

import json
import logging
import time
import urllib.request


from common.connectionhandler import Peer
from common.datatypes import *
from common.firewall import modify_firewall
from common.messages import Login2LauncherNextMapMessage, \
                            Login2LauncherSetPlayerLoadoutsMessage, \
                            Login2LauncherRemovePlayerLoadoutsMessage, \
                            Login2LauncherAddPlayer, \
                            Login2LauncherRemovePlayer, \
                            Login2LauncherPings
from common.statetracer import statetracer, TracingDict
from .player.state.unauthenticated_state import UnauthenticatedState
from .player.state.authenticated_state import AuthenticatedState

PING_UPDATE_TIME = 3


@statetracer('server_id', 'detected_ip', 'address_pair', 'port', 'joinable', 'players', 'player_being_kicked',
             'match_end_time_rel_or_abs', 'match_time_counting', 'be_score', 'ds_score', 'map_id')
class GameServer(Peer):
    def __init__(self, detected_ip: IPv4Address):
        super().__init__()

        self.logger = logging.getLogger(__name__)
        self.login_server = None
        self.server_id = None
        self.match_id = None
        self.detected_ip = detected_ip
        self.address_pair = None
        self.port = None
        self.description = None
        self.motd = None
        self.password_hash = None
        self.region = None

        self.game_setting_mode = 'ootb'

        self.joinable = False
        self.players = TracingDict(refsonly = True)
        self.player_being_kicked = None
        self.match_end_time_rel_or_abs = None
        self.match_time_counting = False
        self.be_score = 0
        self.ds_score = 0
        self.map_id = 0

        if self.detected_ip.is_global:
            response = urllib.request.urlopen('http://tools.keycdn.com/geo.json?host=%s' % self.detected_ip)
            result = response.read()
            json_result = json.loads(result)

            continent_code_to_region = {
                'NA': REGION_NORTH_AMERICA,
                'EU': REGION_EUROPE,
                'OC': REGION_OCEANIA_AUSTRALIA
            }
            try:
                self.region = continent_code_to_region[json_result['data']['geo']['continent_code']]
            except KeyError:
                self.region = REGION_EUROPE
        else:
            self.region = REGION_EUROPE

    def __str__(self):
        return 'GameServer(%d)' % self.server_id

    def disconnect(self, exception=None):
        for player in list(self.players.values()):
            player.set_state(AuthenticatedState)
        super().disconnect(exception)

    def set_info(self, address_pair, game_setting_mode: str, description: str, motd: str, password_hash: bytes):
        self.address_pair = address_pair
        self.game_setting_mode = game_setting_mode
        self.description = description
        self.motd = motd
        self.password_hash = password_hash
        self.send_pings()

    def set_match_time(self, seconds_remaining, counting):
        self.match_time_counting = counting
        if counting:
            self.match_end_time_rel_or_abs = int(time.time() + seconds_remaining)
        else:
            self.match_end_time_rel_or_abs = seconds_remaining

    def set_ready(self, port):
        if port is not None:
            self.be_score = 0
            self.ds_score = 0
            self.port = port
            self.joinable = True

            for unique_id, player in self.players.items():
                b4msg = a00b4().set_server(self).set_player(unique_id)
                b4msg.findbytype(m042a).set(3)
                b4msg.content.append(m02ff())
                player.send(b4msg)

            self.send(Login2LauncherNextMapMessage())
        else:
            self.joinable = False

    def get_time_remaining(self):
        if self.match_end_time_rel_or_abs is not None:
            if self.match_time_counting:
                time_remaining = int(self.match_end_time_rel_or_abs - time.time())
            else:
                time_remaining = self.match_end_time_rel_or_abs
        else:
            time_remaining = 0

        if time_remaining < 0:
            time_remaining = 0

        return time_remaining

    def add_player(self, player):
        assert player.unique_id not in self.players
        self.players[player.unique_id] = player
        player.vote = None
        player_ip = player.address_pair.get_address_seen_from(self.address_pair)
        msg = Login2LauncherAddPlayer(player.unique_id,
                                      str(player_ip) if player_ip is not None else '')
        self.send(msg)

    def remove_player(self, player):
        assert player.unique_id in self.players
        del self.players[player.unique_id]
        player_ip = player.address_pair.get_address_seen_from(self.address_pair)
        msg = Login2LauncherRemovePlayer(player.unique_id,
                                         str(player_ip) if player_ip is not None else '')
        self.send(msg)

    def send_all_players(self, data):
        for player in self.players.values():
            player.send(data)

    def send_all_players_on_team(self, data, team):
        for player in self.players.values():
            if player.team == team:
                player.send(data)

    def set_player_loadouts(self, player):
        assert player.unique_id in self.players
        msg = Login2LauncherSetPlayerLoadoutsMessage(player.unique_id,
                                                     player.get_current_loadouts().get_data())
        self.send(msg)

    def remove_player_loadouts(self, player):
        assert player.unique_id in self.players
        msg = Login2LauncherRemovePlayerLoadoutsMessage(player.unique_id)
        self.send(msg)

    def start_votekick(self, kicker, kickee):
        if kickee.unique_id in self.players and self.player_being_kicked is None:

            # Start a new vote
            reply = a018c()
            reply.content = [
                m02c4().set(self.match_id),
                m034a().set(kickee.display_name),
                m0348().set(kickee.unique_id),
                m02fc().set(STDMSG_VOTE_BY_X_KICK_PLAYER_X_YES_NO),
                m0442(),
                m0704().set(kicker.unique_id),
                m0705().set(kicker.display_name)
            ]
            self.send_all_players(reply)

            for player in self.players.values():
                player.vote = None

            self.player_being_kicked = kickee

            self.logger.info('server: votekick started by %d:"%s" against %d:"%s"' %
                             (kicker.unique_id, kicker.display_name,
                              kickee.unique_id, kickee.display_name))

            self.login_server.pending_callbacks.add(self, 35, self.end_votekick)

    def end_votekick(self):
        if self.player_being_kicked:
            eligible_voters, total_votes, yes_votes = self._tally_votes()
            vote_passed = total_votes >= 4 and yes_votes / total_votes >= 0.5
            self.logger.info('server: votekick %s at timeout with %d/%d/%d (yes/no/abstain) with %d eligible voters out of %d players' %
                  ('passed' if vote_passed else 'failed',
                   yes_votes,
                   total_votes - yes_votes,
                   eligible_voters - total_votes,
                   eligible_voters,
                   len(self.players)))
            self._do_kick(vote_passed)

    def check_votes(self):
        if self.player_being_kicked:
            eligible_voters, total_votes, yes_votes = self._tally_votes()

            # If enough people vote yes, kick immediately. Otherwise wait for a majority at the timeout.
            if yes_votes >= 8 or total_votes == eligible_voters:
                self.logger.info('server: votekick passed immediately %d/%d/%d (yes/no/abstain) with %d eligible voters out of %d players' %
                      (yes_votes,
                       total_votes - yes_votes,
                       eligible_voters - total_votes,
                       eligible_voters,
                       len(self.players)))
                self._do_kick(True)

    def _tally_votes(self):
        eligible_voters_votes = {
            p.address_pair.get_address_seen_from(self.login_server.address_pair): p.vote
            for p in self.players.values()
        }
        votes = {v for v in eligible_voters_votes.values() if v is not None}
        yes_votes = [v for v in votes if v]

        return len(eligible_voters_votes), len(votes), len(yes_votes)

    def _do_kick(self, votekick_passed):
        player_to_kick = self.player_being_kicked

        reply = a018c()
        reply.content = [
            m0348().set(player_to_kick.unique_id),
            m034a().set(player_to_kick.display_name)
        ]

        if votekick_passed:
            reply.content.extend([
                m02fc().set(STDMSG_PLAYER_X_HAS_BEEN_KICKED),
                m0442().set(1)
            ])

        else:
            reply.content.extend([
                m02fc().set(STDMSG_PLAYER_X_WAS_NOT_VOTED_OUT),
                m0442().set(0)
            ])

            self.send_all_players(reply)

        if votekick_passed:
            # TODO: figure out if a real votekick also causes an
            # inconsistency between the menu you see and the one
            # you're really in
            for msg in [a00b0(), a0035().setmainmenu(), a006f()]:
                player_to_kick.send(msg)
            player_to_kick.set_state(UnauthenticatedState)

            ip_to_ban_on_login_server = player_to_kick.address_pair.get_address_seen_from(self.login_server.address_pair)
            modify_firewall('blacklist', 'add', player_to_kick.unique_id, ip_to_ban_on_login_server)

            def remove_blacklist_rule():
                modify_firewall('blacklist', 'remove', player_to_kick.unique_id, ip_to_ban_on_login_server)

            self.login_server.pending_callbacks.add(self.login_server, 8 * 3600, remove_blacklist_rule)

        self.player_being_kicked = None

    def send_pings(self):
        player_pings = {}
        for unique_id, player in self.players.items():
            player_pings[unique_id] = player.pings[self.region] if self.region in player.pings else 999
        self.send(Login2LauncherPings(player_pings))
        self.login_server.pending_callbacks.add(self, PING_UPDATE_TIME, self.send_pings)
