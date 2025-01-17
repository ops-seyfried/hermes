# -*- coding: utf-8 -*-
"""
Core Hermes module which contains all of the logic for the Bot and running the proper
commands based off what modules have been loaded and registered for the bot. The file
also contains some utility functions that are used within the bot, those these
functions may be moved elsewhere as appopriate.
"""
import argparse
import locale
import logging
import os
import random
import re
import signal
import socket
import ssl
import sys
import threading
import time
import irc

from irc.connection import Factory

from .api import GazelleAPI
from .database import GazelleDB
from .irc import IRCBot
from .loader import load_modules
from .utils import get_git_hash, check_pid, load_config, DotDict
from .cache import Cache
from .persist import PersistentStorage

locale.setlocale(locale.LC_ALL, 'en_US.utf8')
__version__ = "0.2.0"

LOGGER = logging.getLogger("hermes")
HERMES_DIR = os.path.join(os.path.expanduser("~"), ".hermes")


def set_verbosity(verbose=0, level=logging.INFO):
    log_format = logging.Formatter("%(asctime)s [%(levelname)-5.5s]  %(message)s")
    if verbose >= 1:
        file_logger = logging.FileHandler(os.path.join(HERMES_DIR, "hermes.log"))
        file_logger.setFormatter(log_format)
        file_logger.setLevel(level)
        LOGGER.addHandler(file_logger)
        if verbose == 2:
            console_logger = logging.StreamHandler(sys.stdout)
            console_logger.setFormatter(log_format)
            console_logger.setLevel(level)
            LOGGER.addHandler(console_logger)
    else:
        LOGGER.addHandler(logging.NullHandler())
    LOGGER.setLevel(level)


# noinspection PyMethodMayBeStatic,PyUnusedLocal
class Hermes(IRCBot):
    def __init__(self):
        self.logger = LOGGER
        self.dir = HERMES_DIR
        self.logger.info("-> Loading Hermes ({})".format(get_version_string()))
        if not os.path.isdir(HERMES_DIR):
            os.makedirs(HERMES_DIR, exist_ok=True)
        self.config = load_config(os.path.join(HERMES_DIR, "config.yml"))
        self.logger.info("-> Loaded Config")
        self.nick = self.config.nick
        self.name = self.config.name if 'name' in self.config else self.nick

        self.logger.info("-> Loading Modules")
        self.modules = load_modules()
        self.logger.info("-> Modules Loaded")

        if 'persist' in self.config and 'path' in self.config.persist:
            persist_path = self.config.persist.path.replace('!HERMES!', self.dir)
        else:
            persist_path = os.path.join(self.dir, 'persist.dat')

        self.storage = PersistentStorage(persist_path)

        self.logger.info("-> Loaded Storage ({0} keys)".format(len(self.storage)))
        if 'cache' not in self.storage:
            self.storage['cache'] = DotDict()

        self.cache = Cache(self.storage['cache'])

        self.logger.info("-> Loaded Cache ({0} keys)".format(len(self.cache)))

        self.listener = None
        self.database = None

        if 'socket' in self.config:
            self.listener = Listener(
                self.config['socket']['host'],
                self.config['socket']['port']
            )

        if 'database' in self.config:
            self.database = GazelleDB(
                self.config.database.host,
                self.config.database.dbname,
                self.config.database.username,
                self.config.database.password
            )
        elif 'api' in self.config:
            self.database = GazelleAPI(
                self.config.site.url,
                self.config.api.id,
                self.config.api.key,
                self.cache
            )

        self.logger.info("-> Loaded DB")

        for name, mod in self.modules.items():
            # noinspection PyBroadException
            try:
                if hasattr(mod, 'setup'):
                    mod.setup(self)
                self.logger.info("Loaded module: {}".format(name))
            except BaseException:
                self.logger.exception("Error Module: {}".format(name))

        if 'ssl' in self.config.irc and self.config.irc.ssl is True:
            factory = Factory(wrapper=ssl.wrap_socket)
        else:
            factory = Factory()

        super().__init__([(self.config.irc.host, self.config.irc.port)],
                         '{}{}'.format(self.nick, random.randint(1, 1000)), self.name,
                         connect_factory=factory)
        for attr in ("on_pubmsg", "on_privmsg"):
            setattr(self, attr, self._dispatch)
        self.logger.info("-> Loaded IRC")

        self.api_poll_heartbeat = self.config.polling.heartbeat
        self.api_poll_threshold = self.config.polling.threshold
        self.api_poll_results = []
        self.api_poll_messaged = False

    def set_nick(self, connection):
        connection.send_raw('NICK {}'.format(self.nick))
        connection.send_raw('SETIDENT {} {}'.format(self.nick, self.nick))
        connection.send_raw("SETHOST {}.{}".format(self.nick, self.config.site.tld))
        if hasattr(self.config.irc, "nickserv"):
            self.logger.info("-> Identifying with NickServ")
            connection.privmsg("NickServ", "IDENTIFY {}".format(
                self.config.irc.nickserv.password)
            )

    def on_nicknameinuse(self, connection, event):
        """
        Executed if someone else has already taken the bot's nickname and we cannot
        take it back via NickServ. Kill the offending user, and take the nick
        through blood.

        :raises: SystemError
        """
        self.logger.info("-> killing user named {}".format(self.nick))
        connection.kill(self.nick)
        self.set_nick(connection)
        # raise SystemError("*** ERROR: Bot's nickname in use! ***")

    def on_erroneusenickname(self, connection, event):
        """
        Executed if the nickname contains illegal characters (such as #) which IRC does
        not support. This is considered a fatal error and should only happen on poor
        configuration.

        :raises: SystemError
        """
        raise SystemError("*** ERROR: Illegal characters in BotNick ***")

    def on_welcome(self, connection, event):
        """
        Executed when the bot connects to the server (and gets the "welcome message").
        We use this to do some initialization routines (like joining the necessary
        channels, etc.) that the bot needs to operate

        :param connection:
        :param event:
        :return:
        """
        self.logger.info("-> Connected to {} with nick {}".format(self.config.irc.host,
                                                                  self.nick))
        if hasattr(self.config.irc, "oper"):
            self.logger.info("-> Setting OPER")
            connection.send_raw("OPER {} {}".format(self.config.irc.oper.name,
                                                    self.config.irc.oper.password))

        self.set_nick(connection)

        if self.listener is not None and not self.listener.is_alive():
            self.listener.set_connection(connection)
            self.listener.start()
        if hasattr(self.config.irc, "channels") and \
                isinstance(self.config.irc.channels, dict):
            for name in self.config.irc.channels:
                self.logger.info("-> Entering {}".format(name))
                connection.send_raw("SAJOIN {} #{}".format(self.nick, name))

    def on_disconnect(self, connection, event):
        self.logger.info("-> Disconnected from IRC")

    def _execute_function(self, func, connection, event):
        cmd = event.cmd
        if hasattr(func, "commands"):
            for command in func.commands:
                if cmd == "." + command or cmd == "!" + command or \
                        (event.type == "privmsg" and cmd == command):
                    func(self, connection, event)
        if hasattr(func, "rules"):
            for rule in func.rules:
                match = re.search(rule, event.msg)
                if match:
                    func(self, connection, event, match)

    def check_admin(self, event):
        return event.source.nick in self.config.admins \
            and event.source.host is not None \
            and event.source.host.endswith(self.config.site.tld) \
            and event.source.host.split(",")[0] not in self.config.admins

    def _dispatch(self, connection, event):
        """
        :param connection:
        :param event: class that contains that describes the IRC event
            type (type of event, always privmsg)
            source (name of who sent the message containing host and nick)
                nick -
                user -
                host -
            target (name of who is recieving the message, in this case the bot name)
            arguments (list of arguments to the event, for this, [0] is message that
                        was sent)
            tags (empty list)
        """
        event.msg = event.arguments[0]
        args = event.arguments[0].split()
        if len(args) == 0:
            return
        event.cmd = args[0].lower()
        event.args = args[1:] if len(args) > 1 else []
        for name, mod in self.modules.items():
            for func in mod.__callables__:
                if func.disabled is True:
                    continue
                elif func.admin_only is True and not self.check_admin(event):
                    continue
                # noinspection PyBroadException
                try:
                    if event.type in func.events:
                        self._execute_function(func, connection, event)
                except BaseException:
                    if event.type == "privmsg":
                        msg = "I'm sorry, {}.{} threw an exception.".format(
                            name,
                            func.__name__
                        )
                        msg += " Please tell an administrator and try again later."
                        connection.privmsg(event.source.nick, msg)
                    self.logger.exception(
                        "Failed to run function: {}.{}".format(name, func.__name__)
                    )

    def disconnect(self, msg="I'll be back!"):
        if self.database is not None:
            self.database.disconnect()
        if self.listener is not None:
            self.listener.stop()
        super(Hermes, self).disconnect(msg)

    def restart(self):
        self.disconnect()
        raise RestartException


class RestartException(Exception):
    pass


class BotCheck(threading.Thread):
    def __init__(self, bot):
        super().__init__()
        self.alive = True
        self.bot = bot

    def run(self):
        time.sleep(120)
        while self.alive:
            if len(self.bot.channels) == 0:
                self.bot.logger.info('-> Bot not connected to channels, restarting')
                self.bot.restart()
                time.sleep(5)

    def stop(self):
        self.alive = False
        self.join()


class SaveData(threading.Thread):
    def __init__(self, bot):
        super().__init__()
        self.alive = True
        self.bot = bot
        self.logger = LOGGER

    def run(self):
        cycle = 480
        while self.alive:
            if cycle >= 600:
                self.logger.info('saving data')
                self.bot.storage.save()
                cycle = 0
            cycle += 1
            time.sleep(1)

    def stop(self):
        self.alive = False
        self.join()

class PollApi(threading.Thread):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    def run(self):
        while True:
            result = False
            user = self.bot.database.get_user(1)
            if user == None:
                result = True
            if len(self.bot.api_poll_results) < self.bot.api_poll_threshold:
                self.bot.api_poll_results = self.bot.api_poll_results + [result]
            else:
                self.bot.api_poll_results = self.bot.api_poll_results[1:] + [result]
            if all(self.bot.api_poll_results) and not self.bot.api_poll_messaged:
                for admin in self.bot.config.admins:
                    self.bot.connection.privmsg(admin, "Bad polls exceeded threshold. Is the site down?")
                self.bot.api_poll_messaged = True
            time.sleep(self.api_poll_heartbeat)

class Listener(threading.Thread):
    """
    Gazelle communicates with the IRC bot through a socket. Gazelle will send things
    like new torrents (via announce) or reports/errors that the bot would then properly
    relay into the appropriate IRC channels.
    """
    def __init__(self, host, port):
        self.logger = LOGGER
        self.running = True
        self.restart = False
        self.connection = None
        self.host = host
        self.port = port
        threading.Thread.__init__(self)

    def set_connection(self, connection):
        self.connection = connection

    def stop(self):
        self.running = False
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.connect((self.host, self.port))
#        client_socket.send("QUITTING")
        client_socket.close()

    def run(self):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.bind((self.host, self.port))
        server_socket.listen(5)
        self.logger.info(
            "-> Listener waiting for connection on port {}".format(self.port)
        )
        while self.running:
            if self.restart:
                server_socket.send("RESTARTING")
                server_socket.close()
                server_socket = socket.socket(
                    socket.AF_INET,
                    socket.SOCK_STREAM
                )
                server_socket.bind((self.host, self.port))
                server_socket.listen(5)
            client_socket, address = server_socket.accept()
            # Only accept 510 bytes as irc module appends b'\r\n' to bring
            # us to max of 512
            data = client_socket.recv(510).decode('utf-8', errors='replace').strip()
            self.logger.info("-> Listener Recieved: {}".format(data))
            client_socket.close()
            try:
                data_details = data.split()
                if len(data_details) < 2:
                    continue
                if data_details[0] in ["/privmsg", "privmsg"] \
                        and data_details[1] == "#":
                    continue
                if self.connection is not None:
                    self.connection.send_raw(data)
            except socket.error as e:
                self.logger.error(
                    "*** Socket Error: %d: %s ***" % (e.args[0], e.args[1])
                )
            except irc.client.MessageTooLong:
                self.logger.warn("-> Skipping input as too long: {}".format(data))
            except irc.client.InvalidCharacters:
                self.logger.warn(
                    "-> Skipping message as contained newlines: {}".format(data)
                )
        server_socket.close()


def get_version_string():
    version_string = __version__
    git_hash = get_git_hash()
    if git_hash is not None:
        version_string += "-{}".format(git_hash)
    return version_string


def _parse_args():
    parser = argparse.ArgumentParser(
        description="CLI for the hermes IRC bot for Gazelle"
    )
    parser.add_argument(
        "-v", "--verbose",
        action='count', default=0,
        help="Define how much logging to do. (-v to file, -vv to stdout)"
    )
    parser.add_argument(
        "--log-level",
        action='store', choices=['debug', 'info', 'warn', 'error'], default='info',
        help="What level of messages should be logged by hermes."
    )
    parser.add_argument(
        "-V", "--version",
        action='version',
        version="%(prog)s ({})".format(get_version_string())
    )
    parser.add_argument(
        "--nofork",
        action="store_true", default=False,
        help="Don't run as forked daemon. (set if using -vv)"
    )
    parser.add_argument(
        "--no-eternal",
        action="store_true", default=False,
        help="No not attempt to restart bot in case of crash"
    )
    parser.add_argument(
        "--stop",
        action='store_true', default=False,
        help='Try and have a previous instance of Hermes gracefully stop'
    )
    parser.add_argument(
        "--kill",
        action='store_true', default=False,
        help="Try and have a previous intance of Hermes exit immediately."
    )
    return parser.parse_args()


def run_hermes():
    args = _parse_args()
    levels = {'debug': logging.DEBUG, 'info': logging.INFO, 'warn': logging.WARN,
              'error': logging.ERROR}
    log_level = levels[args.log_level]
    set_verbosity(args.verbose, log_level)
    run_eternal = args.no_eternal is not True

    if os.getuid() == 0 or os.geteuid() == 0:
        raise SystemExit('Error: Do not run Hermes with root privileges.')

    os.makedirs(HERMES_DIR, exist_ok=True)

    pidfile = os.path.join(HERMES_DIR, "hermes.pid")
    if os.path.isfile(pidfile):
        with open(pidfile, 'r') as open_pidfile:
            try:
                old_pid = int(open_pidfile.read().strip())
            except ValueError:
                old_pid = None
            if old_pid is not None and check_pid(old_pid):
                if args.stop:
                    print("Stopping instance of Hermes ({})".format(old_pid))
                    os.kill(old_pid, signal.SIGTERM)
                elif args.kill:
                    print("Killing instance of Hermes ({})".format(old_pid))
                    os.kill(old_pid, signal.SIGKILL)
                else:
                    raise SystemExit(
                        "{} already exists, exiting".format(pidfile)
                    )
                if os.path.isfile(pidfile):
                    os.unlink(pidfile)
                raise SystemExit(0)
            elif args.stop or args.kill:
                raise SystemExit("Hermes is not currently running.")

    # If we have set -vv, then we will not run as a daemon
    # as we'll assume you wanted
    # to see the console output.
    if args.nofork is not True and args.verbose < 2:
        child_pid = os.fork()
        if child_pid != 0:
            raise SystemExit

    with open(pidfile, 'w') as open_pidfile:
        open_pidfile.write(str(os.getpid()))

    irc.client.ServerConnection.buffer_class.errors = 'replace'

    last_run = None
    save_thread = None
    try:
        hermes = Hermes()
        api_poller = PollApi(hermes)
        api_poller.start()
        save_thread = SaveData(hermes)
        save_thread.start()
        # thread = BotCheck(hermes)
        # thread.start()

        def signal_handler(sig, _):
            if sig is signal.SIGTERM:
                hermes.disconnect()
            else:
                hermes.die()

        signal.signal(signal.SIGTERM, signal_handler)
        # signal.signal(signal.SIGKILL, signal_handler)
        while run_eternal:
            # noinspection PyBroadException
            try:
                last_run = time.time()
                hermes.start()
            except (KeyboardInterrupt, SystemError, SystemExit) as e:
                LOGGER.info("-> {}".format(e))
                hermes.disconnect("Leaving...")
                LOGGER.info("Quitting bot")
                break
            except RestartException:
                # thread.stop()
                time.sleep(5)
                hermes = Hermes()
                # thread = BotCheck(hermes)
                # thread.start()
                hermes.start()
            except BaseException:
                if last_run > time.time() - 5:
                    hermes.disconnect("Crashed, going offline.")
                    run_eternal = False
                else:
                    hermes.disconnect("Crashed, going to reboot...")
                    time.sleep(2)
                LOGGER.exception("Crash")
    finally:
        if save_thread is not None:
            save_thread.stop()
        if os.path.isfile(pidfile):
            os.unlink(pidfile)
