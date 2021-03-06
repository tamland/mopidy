from __future__ import absolute_import, unicode_literals

import logging
import re

import pykka

from mopidy.mpd import exceptions, protocol, tokenize

logger = logging.getLogger(__name__)

protocol.load_protocol_modules()


class MpdDispatcher(object):
    """
    The MPD session feeds the MPD dispatcher with requests. The dispatcher
    finds the correct handler, processes the request and sends the response
    back to the MPD session.
    """

    _noidle = re.compile(r'^noidle$')

    def __init__(self, session=None, config=None, core=None):
        self.config = config
        self.authenticated = False
        self.command_list_receiving = False
        self.command_list_ok = False
        self.command_list = []
        self.command_list_index = None
        self.context = MpdContext(
            self, session=session, config=config, core=core)

    def handle_request(self, request, current_command_list_index=None):
        """Dispatch incoming requests to the correct handler."""
        self.command_list_index = current_command_list_index
        response = []
        filter_chain = [
            self._catch_mpd_ack_errors_filter,
            self._authenticate_filter,
            self._command_list_filter,
            self._idle_filter,
            self._add_ok_filter,
            self._call_handler_filter,
        ]
        return self._call_next_filter(request, response, filter_chain)

    def handle_idle(self, subsystem):
        self.context.events.add(subsystem)

        subsystems = self.context.subscriptions.intersection(
            self.context.events)
        if not subsystems:
            return

        response = []
        for subsystem in subsystems:
            response.append('changed: %s' % subsystem)
        response.append('OK')
        self.context.subscriptions = set()
        self.context.events = set()
        self.context.session.send_lines(response)

    def _call_next_filter(self, request, response, filter_chain):
        if filter_chain:
            next_filter = filter_chain.pop(0)
            return next_filter(request, response, filter_chain)
        else:
            return response

    # Filter: catch MPD ACK errors

    def _catch_mpd_ack_errors_filter(self, request, response, filter_chain):
        try:
            return self._call_next_filter(request, response, filter_chain)
        except exceptions.MpdAckError as mpd_ack_error:
            if self.command_list_index is not None:
                mpd_ack_error.index = self.command_list_index
            return [mpd_ack_error.get_mpd_ack()]

    # Filter: authenticate

    def _authenticate_filter(self, request, response, filter_chain):
        if self.authenticated:
            return self._call_next_filter(request, response, filter_chain)
        elif self.config['mpd']['password'] is None:
            self.authenticated = True
            return self._call_next_filter(request, response, filter_chain)
        else:
            command_name = request.split(' ')[0]
            command = protocol.commands.handlers.get(command_name)
            if command and not command.auth_required:
                return self._call_next_filter(request, response, filter_chain)
            else:
                raise exceptions.MpdPermissionError(command=command_name)

    # Filter: command list

    def _command_list_filter(self, request, response, filter_chain):
        if self._is_receiving_command_list(request):
            self.command_list.append(request)
            return []
        else:
            response = self._call_next_filter(request, response, filter_chain)
            if (self._is_receiving_command_list(request) or
                    self._is_processing_command_list(request)):
                if response and response[-1] == 'OK':
                    response = response[:-1]
            return response

    def _is_receiving_command_list(self, request):
        return (
            self.command_list_receiving and request != 'command_list_end')

    def _is_processing_command_list(self, request):
        return (
            self.command_list_index is not None and
            request != 'command_list_end')

    # Filter: idle

    def _idle_filter(self, request, response, filter_chain):
        if self._is_currently_idle() and not self._noidle.match(request):
            logger.debug(
                'Client sent us %s, only %s is allowed while in '
                'the idle state', repr(request), repr('noidle'))
            self.context.session.close()
            return []

        if not self._is_currently_idle() and self._noidle.match(request):
            return []  # noidle was called before idle

        response = self._call_next_filter(request, response, filter_chain)

        if self._is_currently_idle():
            return []
        else:
            return response

    def _is_currently_idle(self):
        return bool(self.context.subscriptions)

    # Filter: add OK

    def _add_ok_filter(self, request, response, filter_chain):
        response = self._call_next_filter(request, response, filter_chain)
        if not self._has_error(response):
            response.append('OK')
        return response

    def _has_error(self, response):
        return response and response[-1].startswith('ACK')

    # Filter: call handler

    def _call_handler_filter(self, request, response, filter_chain):
        try:
            response = self._format_response(self._call_handler(request))
            return self._call_next_filter(request, response, filter_chain)
        except pykka.ActorDeadError as e:
            logger.warning('Tried to communicate with dead actor.')
            raise exceptions.MpdSystemError(e)

    def _call_handler(self, request):
        tokens = tokenize.split(request)
        try:
            return protocol.commands.call(tokens, context=self.context)
        except exceptions.MpdAckError as exc:
            if exc.command is None:
                exc.command = tokens[0]
            raise

    def _format_response(self, response):
        formatted_response = []
        for element in self._listify_result(response):
            formatted_response.extend(self._format_lines(element))
        return formatted_response

    def _listify_result(self, result):
        if result is None:
            return []
        if isinstance(result, set):
            return self._flatten(list(result))
        if not isinstance(result, list):
            return [result]
        return self._flatten(result)

    def _flatten(self, the_list):
        result = []
        for element in the_list:
            if isinstance(element, list):
                result.extend(self._flatten(element))
            else:
                result.append(element)
        return result

    def _format_lines(self, line):
        if isinstance(line, dict):
            return ['%s: %s' % (key, value) for (key, value) in line.items()]
        if isinstance(line, tuple):
            (key, value) = line
            return ['%s: %s' % (key, value)]
        return [line]


class MpdContext(object):
    """
    This object is passed as the first argument to all MPD command handlers to
    give the command handlers access to important parts of Mopidy.
    """

    #: The current :class:`MpdDispatcher`.
    dispatcher = None

    #: The current :class:`mopidy.mpd.MpdSession`.
    session = None

    #: The MPD password
    password = None

    #: The Mopidy core API. An instance of :class:`mopidy.core.Core`.
    core = None

    #: The active subsystems that have pending events.
    events = None

    #: The subsytems that we want to be notified about in idle mode.
    subscriptions = None

    _invalid_browse_chars = re.compile(r'[\n\r]')
    _invalid_playlist_chars = re.compile(r'[/]')

    def __init__(self, dispatcher, session=None, config=None, core=None):
        self.dispatcher = dispatcher
        self.session = session
        if config is not None:
            self.password = config['mpd']['password']
        self.core = core
        self.events = set()
        self.subscriptions = set()
        self._uri_from_name = {}
        self._name_from_uri = {}
        self.refresh_playlists_mapping()

    def create_unique_name(self, name, uri):
        stripped_name = self._invalid_browse_chars.sub(' ', name)
        name = stripped_name
        i = 2
        while name in self._uri_from_name:
            if self._uri_from_name[name] == uri:
                return name
            name = '%s [%d]' % (stripped_name, i)
            i += 1
        return name

    def insert_name_uri_mapping(self, name, uri):
        name = self.create_unique_name(name, uri)
        self._uri_from_name[name] = uri
        self._name_from_uri[uri] = name
        return name

    def refresh_playlists_mapping(self):
        """
        Maintain map between playlists and unique playlist names to be used by
        MPD
        """
        if self.core is not None:
            for playlist in self.core.playlists.playlists.get():
                if not playlist.name:
                    continue
                # TODO: add scheme to name perhaps 'foo (spotify)' etc.
                name = self._invalid_playlist_chars.sub('|', playlist.name)
                self.insert_name_uri_mapping(name, playlist.uri)

    def lookup_playlist_from_name(self, name):
        """
        Helper function to retrieve a playlist from its unique MPD name.
        """
        if not self._uri_from_name:
            self.refresh_playlists_mapping()
        if name not in self._uri_from_name:
            return None
        uri = self._uri_from_name[name]
        return self.core.playlists.lookup(uri).get()

    def lookup_playlist_name_from_uri(self, uri):
        """
        Helper function to retrieve the unique MPD playlist name from its uri.
        """
        if uri not in self._name_from_uri:
            self.refresh_playlists_mapping()
        return self._name_from_uri[uri]

    def browse(self, path, recursive=True, lookup=True):
        path_parts = re.findall(r'[^/]+', path or '')
        root_path = '/'.join([''] + path_parts)

        if root_path not in self._uri_from_name:
            uri = None
            for part in path_parts:
                for ref in self.core.library.browse(uri).get():
                    if ref.type != ref.TRACK and ref.name == part:
                        uri = ref.uri
                        break
                else:
                    raise exceptions.MpdNoExistError('Not found')
            root_path = self.insert_name_uri_mapping(root_path, uri)

        else:
            uri = self._uri_from_name[root_path]

        if recursive:
            yield (root_path, None)

        path_and_futures = [(root_path, self.core.library.browse(uri))]
        while path_and_futures:
            base_path, future = path_and_futures.pop()
            for ref in future.get():
                path = '/'.join([base_path, ref.name.replace('/', '')])
                path = self.insert_name_uri_mapping(path, ref.uri)

                if ref.type == ref.TRACK:
                    if lookup:
                        yield (path, self.core.library.lookup(ref.uri))
                    else:
                        yield (path, ref)
                else:
                    yield (path, None)
                    if recursive:
                        path_and_futures.append(
                            (path, self.core.library.browse(ref.uri)))
