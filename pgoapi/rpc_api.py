"""
pgoapi - Pokemon Go API
Copyright (c) 2016 tjado <https://github.com/tejado>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
OR OTHER DEALINGS IN THE SOFTWARE.

Author: tjado <https://github.com/tejado>
"""

from __future__ import absolute_import

import os
import random
import logging
import requests
import subprocess
import ctypes

from importlib import import_module

from google.protobuf import message
from protobuf_to_dict import protobuf_to_dict
from pycrypt import pycrypt

from pgoapi.exceptions import (AuthTokenExpiredException, BadRequestException, MalformedNianticResponseException, NianticIPBannedException, NianticOfflineException, NianticThrottlingException, NianticTimeoutException, NotLoggedInException, ServerApiEndpointRedirectException, UnexpectedResponseException)
from pgoapi.utilities import to_camel_case, get_time, get_format_time_diff, weighted_choice
from pgoapi.hash_server import HashServer

from . import protos
from pogoprotos.networking.envelopes.request_envelope_pb2 import RequestEnvelope
from pogoprotos.networking.envelopes.response_envelope_pb2 import ResponseEnvelope
from pogoprotos.networking.requests.request_type_pb2 import RequestType
from pogoprotos.networking.platform.platform_request_type_pb2 import PlatformRequestType
from pogoprotos.networking.envelopes.signature_pb2 import Signature
from pogoprotos.networking.platform.requests.send_encrypted_signature_request_pb2 import SendEncryptedSignatureRequest
from pogoprotos.networking.platform.requests.unknown_ptr8_request_pb2 import UnknownPtr8Request


class RpcApi:
    def __init__(self, auth_provider, device_info, state, request_id, start_time):

        self.log = logging.getLogger(__name__)

        self._auth_provider = auth_provider
        self.request_id = request_id
        self.start_time = start_time

        # mystical unknown6 - resolved by PokemonGoDev
        self._hash_engine = None
        self.request_proto = None

        # data fields for SignalAgglom
        self.token2 = random.randint(1, 59)
        self.course = random.uniform(0, 360)

        self.state = state
        self.device_info = device_info

    def activate_hash_server(self, auth_token):
        self._hash_engine = HashServer(auth_token)

    def decode_raw(self, raw):
        output = error = None
        try:
            process = subprocess.Popen(
                ['protoc', '--decode_raw'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True)
            output, error = process.communicate(raw)
        except (subprocess.SubprocessError, OSError):
            output = "Couldn't find protoc in your environment OR other issue..."

        return output

    def get_class(self, cls):
        module_, class_ = cls.rsplit('.', 1)
        class_ = getattr(import_module(module_), to_camel_case(class_))
        return class_

    def _make_rpc(self, endpoint, request_proto_plain):
        self.log.debug('Execution of RPC')

        request_proto_serialized = request_proto_plain.SerializeToString()
        try:
            http_response = self._session.post(
                endpoint, data=request_proto_serialized, timeout=30)
        except requests.exceptions.Timeout:
            raise NianticTimeoutException('RPC request timed out.')
        except requests.exceptions.ConnectionError as e:
            raise NianticOfflineException(e)

        return http_response

    def request(self,
                endpoint,
                subrequests,
                platforms,
                player_position,
                use_dict=True):

        if not self._auth_provider or self._auth_provider.is_login() is False:
            raise NotLoggedInException()

        self.request_proto = self.request_proto or self._build_main_request(
            subrequests, platforms, player_position)
        response = self._make_rpc(endpoint, self.request_proto)

        response_dict = self._parse_main_response(response, subrequests,
                                                  use_dict)

        # some response validations
        if isinstance(response_dict, dict):
            if use_dict:
                status_code = response_dict.get('status_code')
                if ('auth_ticket' in response_dict) and (
                        'expire_timestamp_ms' in response_dict['auth_ticket']):
                    ticket = response_dict['auth_ticket']
                    self.check_authentication(ticket['expire_timestamp_ms'],
                                              ticket['start'], ticket['end'])
            else:
                status_code = response_dict['envelope'].status_code
                ticket = response_dict['envelope'].auth_ticket
                if ticket:
                    self.check_authentication(ticket.expire_timestamp_ms,
                                              ticket.start, ticket.end)

            if status_code == 102:
                raise AuthTokenExpiredException
            elif status_code == 52:
                raise NianticThrottlingException(
                    "Request throttled by server... slow down man")
            elif status_code == 53:
                api_url = response_dict.get('api_url')
                if api_url:
                    exception = ServerApiEndpointRedirectException()
                    exception.set_redirected_endpoint(api_url)
                    raise exception
                else:
                    raise UnexpectedResponseException

        return response_dict

    def check_authentication(self, expire_timestamp_ms, start, end):
        if self._auth_provider.is_new_ticket(expire_timestamp_ms):

            had_ticket = self._auth_provider.has_ticket()
            self._auth_provider.set_ticket([expire_timestamp_ms, start, end])

            now_ms = get_time(ms=True)
            h, m, s = get_format_time_diff(now_ms, expire_timestamp_ms, True)

            if had_ticket:
                self.log.debug(
                    'Replacing old Session Ticket with new one valid for %02d:%02d:%02d hours (%s < %s)',
                    h, m, s, now_ms, expire_timestamp_ms)
            else:
                self.log.debug(
                    'Received Session Ticket valid for %02d:%02d:%02d hours (%s < %s)',
                    h, m, s, now_ms, expire_timestamp_ms)

    def _build_main_request(self, subrequests, platforms,
                            player_position=None):
        self.log.debug('Generating main RPC request...')

        request = RequestEnvelope()
        request.status_code = 2
        request.request_id = self.request_id
        # 5: 43%, 10: 30%, 30: 5%, 50: 4%, 65: 10%, 200: 1%, float: 7%
        request.accuracy = weighted_choice([
            (5, 43),
            (10, 30),
            (30, 5),
            (50, 4),
            (65, 10),
            (200, 1),
            (random.uniform(65, 200), 7)
        ])

        if player_position:
            request.latitude, request.longitude, altitude = player_position

        # generate sub requests before Signature generation
        request = self._build_sub_requests(request, subrequests)
        request = self._build_platform_requests(request, platforms)

        ticket = self._auth_provider.get_ticket()
        if ticket:
            self.log.debug(
                'Found Session Ticket - using this instead of oauth token')
            request.auth_ticket.expire_timestamp_ms, request.auth_ticket.start, request.auth_ticket.end = ticket
            ticket_serialized = request.auth_ticket.SerializeToString()

        else:
            self.log.debug(
                'No Session Ticket found - using OAUTH Access Token')
            auth_provider = self._auth_provider
            request.auth_info.provider = auth_provider.get_name()
            request.auth_info.token.contents = auth_provider.get_access_token()
            request.auth_info.token.unknown2 = self.token2
            # Sig uses this when no auth_ticket available.
            ticket_serialized = request.auth_info.SerializeToString()

        sig = Signature()

        sig.session_hash = self.state.session_hash
        sig.timestamp = get_time(ms=True)
        sig.timestamp_since_start = get_time(ms=True) - self.start_time

        self._hash_engine.hash(sig.timestamp, request.latitude,
                               request.longitude, request.accuracy,
                               ticket_serialized, sig.session_hash,
                               request.requests)
        sig.location_hash1 = self._hash_engine.get_location_auth_hash()
        sig.location_hash2 = self._hash_engine.get_location_hash()
        for req_hash in self._hash_engine.get_request_hashes():
            sig.request_hash.append(ctypes.c_uint64(req_hash).value)

        loc = sig.location_fix.add()
        sen = sig.sensor_info.add()

        sen.timestamp_snapshot = sig.timestamp_since_start - int(random.triangular(93, 4900, 3000))
        loc.timestamp_snapshot = sig.timestamp_since_start - int(random.triangular(320, 3000, 1000))

        loc.provider = 'fused'
        loc.latitude = request.latitude
        loc.longitude = request.longitude

        loc.altitude = altitude or random.uniform(150, 250)

        if random.random() > .85:
            # no reading for roughly 1 in 7 updates
            loc.course = -1
            loc.speed = -1
        else:
            loc.course = self.state.course
            loc.speed = random.triangular(0.25, 9.7, 8.2)

        loc.provider_status = 3
        loc.location_type = 1
        if isinstance(request.accuracy, float):
            loc.horizontal_accuracy = weighted_choice([
                (request.accuracy, 50),
                (65, 40),
                (200, 10)
            ])
            loc.vertical_accuracy = weighted_choice([
                (random.uniform(10, 96), 50),
                (10, 34),
                (12, 5),
                (16, 3),
                (24, 4),
                (32, 2),
                (48, 1),
                (96, 1)
            ])
        else:
            loc.horizontal_accuracy = request.accuracy
            if request.accuracy >= 10:
                loc.vertical_accuracy = weighted_choice([
                    (6, 4),
                    (8, 34),
                    (10, 35),
                    (12, 11),
                    (16, 4),
                    (24, 8),
                    (32, 3),
                    (48, 1)
                ])
            else:
                loc.vertical_accuracy = weighted_choice([
                    (3, 15),
                    (4, 39),
                    (6, 14),
                    (8, 13),
                    (10, 14),
                    (12, 5)
                ])

        sen.magnetic_field_accuracy = weighted_choice([
            (-1, 8),
            (0, 2),
            (1, 42),
            (2, 48)
        ])
        if sen.magnetic_field_accuracy == -1:
            sen.magnetic_field_x = 0
            sen.magnetic_field_y = 0
            sen.magnetic_field_z = 0
        else:
            sen.magnetic_field_x = self.state.magnetic_field_x
            sen.magnetic_field_y = self.state.magnetic_field_y
            sen.magnetic_field_z = self.state.magnetic_field_z

        sen.linear_acceleration_x = random.triangular(-1.5, 2.5, 0)
        sen.linear_acceleration_y = random.triangular(-1.2, 1.4, 0)
        sen.linear_acceleration_z = random.triangular(-1.4, .9, 0)
        sen.attitude_pitch = random.triangular(-1.56, 1.57, 0.475)
        sen.attitude_yaw = random.triangular(-1.56, 3.14, .1)
        sen.attitude_roll = random.triangular(-3.14, 3.14, 0)
        sen.rotation_rate_x = random.triangular(-3.2, 3.52, 0)
        sen.rotation_rate_y = random.triangular(-3.1, 4.88, 0)
        sen.rotation_rate_z = random.triangular(-6, 3.7, 0)
        sen.gravity_x = random.triangular(-1, 1, 0.01)
        sen.gravity_y = random.triangular(-1, 1, -.4)
        sen.gravity_z = random.triangular(-1, 1, -.4)
        sen.status = 3

        sig.unknown25 = 3081064678568720862

        if self.device_info:
            for key in self.device_info:
                setattr(sig.device_info, key, self.device_info[key])
            if self.device_info['device_brand'] == 'Apple':
                sig.activity_status.stationary = True
        else:
            sig.activity_status.stationary = True

        signature_proto = sig.SerializeToString()

        if self._needsPtr8(subrequests):
            plat_eight = UnknownPtr8Request()
            plat_eight.message = '15c79df0558009a4242518d2ab65de2a59e09499'
            plat8 = request.platform_requests.add()
            plat8.type = 8
            plat8.request_message = plat_eight.SerializeToString()

        sig_request = SendEncryptedSignatureRequest()
        sig_request.encrypted_signature = pycrypt(signature_proto,
                                                  sig.timestamp_since_start)
        plat = request.platform_requests.add()
        plat.type = 6
        plat.request_message = sig_request.SerializeToString()

        request.ms_since_last_locationfix = sig.timestamp_since_start - loc.timestamp_snapshot

        self.log.debug('Generated protobuf request: \n\r%s', request)

        return request

    def _needsPtr8(self, requests):
        if len(requests) == 0:
            return False
        randval = random.uniform(0, 1)
        rtype, _ = requests[0]
        # GetMapObjects or GetPlayer: 50%
        # Encounter: 10%
        # Others: 3%
        if ((rtype in (2, 106) and randval > 0.5)
                or (rtype == 102 and randval > 0.9) or randval > 0.97):
            return True
        return False

    def _build_sub_requests(self, mainrequest, subrequest_list):
        self.log.debug('Generating sub RPC requests...')

        for entry_id, params in subrequest_list:
            if params:
                entry_name = RequestType.Name(entry_id)
                proto_name = entry_name.lower() + '_message'
                bytes = self._get_proto_bytes(
                    'pogoprotos.networking.requests.messages.', proto_name,
                    params)

                subrequest = mainrequest.requests.add()
                subrequest.request_type = entry_id
                subrequest.request_message = bytes

            else:
                subrequest = mainrequest.requests.add()
                subrequest.request_type = entry_id

        return mainrequest

    def _build_platform_requests(self, mainrequest, platform_list):
        self.log.debug('Generating platform RPC requests...')

        for entry_id, params in platform_list:
            if params:
                entry_name = PlatformRequestType.Name(entry_id)
                if entry_name == 'UNKNOWN_PTR_8':
                    entry_name = 'UNKNOWN_PTR8'
                proto_name = entry_name.lower() + '_request'
                bytes = self._get_proto_bytes(
                    'pogoprotos.networking.platform.requests.', proto_name,
                    params)

                platform = mainrequest.platform_requests.add()
                platform.type = entry_id
                platform.request_message = bytes

            else:
                platform = mainrequest.platform_requests.add()
                platform.type = entry_id

        return mainrequest

    def _get_proto_bytes(self, path, name, entry_content):
        proto_classname = path + name + '_pb2.' + name
        proto = self.get_class(proto_classname)()

        self.log.debug("Subrequest class: %s", proto_classname)

        for key, value in entry_content.items():
            if isinstance(value, list):
                self.log.debug("Found list: %s - trying as repeated", key)
                for i in value:
                    try:
                        self.log.debug("%s -> %s", key, i)
                        r = getattr(proto, key)
                        r.append(i)
                    except Exception as e:
                        self.log.warning(
                            'Argument %s with value %s unknown inside %s (Exception: %s)',
                            key, i, proto_classname, e)
            elif isinstance(value, dict):
                for k in value.keys():
                    try:
                        r = getattr(proto, key)
                        setattr(r, k, value[k])
                    except Exception as e:
                        self.log.warning(
                            'Argument %s with value %s unknown inside %s (Exception: %s)',
                            key, str(value), proto_classname, e)
            else:
                try:
                    setattr(proto, key, value)
                except Exception as e:
                    try:
                        self.log.debug("%s -> %s", key, value)
                        r = getattr(proto, key)
                        r.append(value)
                    except Exception as e:
                        self.log.warning(
                            'Argument %s with value %s unknown inside %s (Exception: %s)',
                            key, value, proto_classname, e)

        return proto.SerializeToString()

    def _parse_main_response(self, response_raw, subrequests, use_dict=True):
        self.log.debug('Parsing main RPC response...')

        if response_raw.status_code == 400:
            raise BadRequestException("400: Bad Request")
        if response_raw.status_code == 403:
            raise NianticIPBannedException(
                "Seems your IP Address is banned or something else went badly wrong..."
            )
        elif response_raw.status_code in (502, 503, 504):
            raise NianticOfflineException(
                '{} Server Error'.format(response_raw.status_code))
        elif response_raw.status_code != 200:
            error = 'Unexpected HTTP server response - needs 200 got {}'.format(
                response_raw.status_code)
            self.log.warning(error)
            self.log.debug('HTTP output: \n%s',
                           response_raw.content.decode('utf-8'))
            raise UnexpectedResponseException(error)

        if not response_raw.content:
            self.log.warning('Empty server response!')
            raise MalformedNianticResponseException('Empty server response!')

        response_proto = ResponseEnvelope()
        try:
            response_proto.ParseFromString(response_raw.content)
        except message.DecodeError as e:
            self.log.error('Could not parse response: %s', e)
            raise MalformedNianticResponseException(
                'Could not decode response.')

        self.log.debug('Protobuf structure of rpc response:\n\r%s',
                       response_proto)
        try:
            self.log.debug(
                'Decode raw over protoc (protoc has to be in your PATH):\n\r%s',
                self.decode_raw(response_raw.content).decode('utf-8'))
        except Exception:
            self.log.debug('Error during protoc parsing - ignored.')

        if use_dict:
            response_proto_dict = protobuf_to_dict(response_proto)
            if 'returns' in response_proto_dict:
                del response_proto_dict['returns']
        else:
            response_proto_dict = {'envelope': response_proto}

        if not response_proto_dict:
            raise MalformedNianticResponseException(
                'Could not convert protobuf to dict.')

        response_proto_dict = self._parse_sub_responses(
            response_proto, subrequests, response_proto_dict, use_dict)

        # It can't be done before.
        if not use_dict:
            del response_proto_dict['envelope'].returns[:]

        return response_proto_dict

    def _parse_sub_responses(self,
                             response_proto,
                             subrequests_list,
                             response_proto_dict,
                             use_dict=True):
        self.log.debug('Parsing sub RPC responses...')
        response_proto_dict['responses'] = {}

        if response_proto.status_code == 53:
            exception = ServerApiEndpointRedirectException()
            exception.set_redirected_endpoint(response_proto.api_url)
            raise exception

        i = 0
        for subresponse in response_proto.returns:
            entry_id, _ = subrequests_list[i]
            entry_name = RequestType.Name(entry_id)
            proto_name = entry_name.lower() + '_response'
            proto_classname = 'pogoprotos.networking.responses.' + proto_name + '_pb2.' + proto_name

            self.log.debug("Parsing class: %s", proto_classname)

            subresponse_return = None
            try:
                subresponse_extension = self.get_class(proto_classname)()
            except Exception:
                subresponse_extension = None
                error = 'Protobuf definition for {} not found'.format(
                    proto_classname)
                subresponse_return = error
                self.log.warning(error)

            if subresponse_extension:
                try:
                    subresponse_extension.ParseFromString(subresponse)
                    if use_dict:

                        subresponse_return = protobuf_to_dict(
                            subresponse_extension)
                    else:
                        subresponse_return = subresponse_extension
                except Exception:
                    error = "Protobuf definition for {} seems not to match".format(
                        proto_classname)
                    subresponse_return = error
                    self.log.warning(error)

            response_proto_dict['responses'][entry_name] = subresponse_return
            i += 1

        return response_proto_dict


# Original by Noctem.
class RpcState:
    def __init__(self):
        self.session_hash = os.urandom(16)
        self.mag_x_min = random.uniform(-80, 60)
        self.mag_x_max = self.mag_x_min + 20
        self.mag_y_min = random.uniform(-120, 90)
        self.mag_y_max = self.mag_y_min + 30
        self.mag_z_min = random.uniform(-70, 40)
        self.mag_z_max = self.mag_y_min + 15
        self._course = random.uniform(0, 359.99)

    @property
    def magnetic_field_x(self):
        return random.uniform(self.mag_x_min, self.mag_x_max)

    @property
    def magnetic_field_y(self):
        return random.uniform(self.mag_y_min, self.mag_y_max)

    @property
    def magnetic_field_z(self):
        return random.uniform(self.mag_z_min, self.mag_z_max)

    @property
    def course(self):
        self._course = random.triangular(0, 359.99, self._course)
        return self._course
