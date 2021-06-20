# -*- coding: utf-8 -*-

import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from TwitchChannelPointsMiner.classes.AnalyticsServer import AnalyticsServer
from TwitchChannelPointsMiner.classes.Chat import ThreadChat
from TwitchChannelPointsMiner.classes.entities.PubsubTopic import PubsubTopic
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Exceptions import StreamerDoesNotExistException
from TwitchChannelPointsMiner.classes.Settings import Priority, Settings
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.logger import LoggerSettings, configure_loggers
from TwitchChannelPointsMiner.utils import (
    _millify,
    at_least_one_value_in_settings_is,
    get_user_agent,
    internet_connection_available,
    set_default_settings,
)

# Suppress:
#   - chardet.charsetprober - [feed]
#   - chardet.charsetprober - [get_confidence]
#   - requests - [Starting new HTTPS connection (1)]
#   - Flask (werkzeug) logs
#   - irc.client - [process_data]
#   - irc.client - [_dispatcher]
#   - irc.client - [_handle_message]
logging.getLogger("chardet.charsetprober").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("irc.client").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


class TwitchChannelPointsMiner:
    __slots__ = [
        "username",
        "twitch",
        "claim_drops_startup",
        "refresh_streamers",
        "priority",
        "streamers",
        "events_predictions",
        "minute_watcher_thread",
        "sync_campaigns_thread",
        "ws_pool",
        "session_id",
        "running",
        "start_datetime",
        "base_points",
        "sources_usernames",
        "logs_file",
    ]

    def __init__(
        self,
        username: str,
        password: str = None,
        claim_drops_startup: bool = False,
        refresh_streamers: float = float("inf"),  # Minutes
        # Settings for logging and selenium as you can see.
        priority: list = [Priority.STREAK, Priority.DROPS, Priority.ORDER],
        # This settings will be global shared trought Settings class
        logger_settings: LoggerSettings = LoggerSettings(),
        # Default values for all streamers
        streamer_settings: StreamerSettings = StreamerSettings(),
    ):
        Settings.analytics_path = os.path.join(Path().absolute(), "analytics", username)
        Path(Settings.analytics_path).mkdir(parents=True, exist_ok=True)

        self.username = username

        # Set as global config
        Settings.logger = logger_settings

        # Init as default all the missing values
        streamer_settings.default()
        streamer_settings.bet.default()
        Settings.streamer_settings = streamer_settings

        user_agent = get_user_agent("FIREFOX")
        self.twitch = Twitch(self.username, user_agent, password)

        self.claim_drops_startup = claim_drops_startup
        self.refresh_streamers = refresh_streamers
        self.priority = priority if isinstance(priority, list) else [priority]

        self.streamers = []
        self.events_predictions = {}
        self.minute_watcher_thread = None
        self.sync_campaigns_thread = None
        self.ws_pool = None

        self.session_id = str(uuid.uuid4())
        self.running = False
        self.start_datetime = None
        self.base_points = {}
        self.sources_usernames = {
            "JSON_FILE": [],
            "FOLLOWERS": [],
        }

        self.logs_file = configure_loggers(self.username, logger_settings)

        for sign in [signal.SIGINT, signal.SIGSEGV, signal.SIGTERM]:
            signal.signal(sign, self.end)

    def analytics(self, host: str = "127.0.0.1", port: int = 5000, refresh: int = 5):
        http_server = AnalyticsServer(host=host, port=port, refresh=refresh)
        http_server.daemon = True
        http_server.name = "Analytics Thread"
        http_server.start()

    def mine(
        self,
        streamers: list = [],
        streamers_json: str = None,
        blacklist: list = [],
        followers=False,
    ):
        self.run(
            streamers=streamers,
            streamers_json=streamers_json,
            blacklist=blacklist,
            followers=followers,
        )

    def run(
        self,
        streamers: list = [],
        streamers_json: str = None,
        blacklist: list = [],
        followers=False,
    ):
        if self.running:
            logger.error("You can't start multiple sessions of this instance!")
        else:
            logger.info(
                f"Start session: '{self.session_id}'", extra={"emoji": ":bomb:"}
            )
            self.running = True
            self.start_datetime = datetime.now()

            self.twitch.login()

            if self.claim_drops_startup is True:
                self.twitch.claim_all_drops_from_inventory()

            self.streamers = self.__reload_streamers(
                streamers=streamers,
                streamers_json=streamers_json,
                blacklist=blacklist,
                followers=followers,
            )
            self.__save_original()

            # If we have at least one streamer with settings = make_predictions True
            make_predictions = at_least_one_value_in_settings_is(
                self.streamers, "make_predictions", True
            )

            # If we have at least one streamer with settings = claim_drops True
            # Spawn a thread for sync inventory and dashboard
            if (
                at_least_one_value_in_settings_is(self.streamers, "claim_drops", True)
                is True
            ):
                self.sync_campaigns_thread = threading.Thread(
                    target=self.twitch.sync_campaigns,
                    args=(self.streamers,),
                )
                self.sync_campaigns_thread.name = "Sync campaigns/inventory"
                self.sync_campaigns_thread.start()
                time.sleep(30)

            self.minute_watcher_thread = threading.Thread(
                target=self.twitch.send_minute_watched_events,
                args=(self.streamers, self.priority),
            )
            self.minute_watcher_thread.name = "Minute watcher"
            self.minute_watcher_thread.start()

            self.ws_pool = WebSocketsPool(
                twitch=self.twitch,
                streamers=self.streamers,
                events_predictions=self.events_predictions,
            )

            # Subscribe to community-points-user. Get update for points spent or gains
            self.ws_pool.submit(
                PubsubTopic(
                    "community-points-user-v1",
                    user_id=self.twitch.twitch_login.get_user_id(),
                )
            )

            # Going to subscribe to predictions-user-v1. Get update when we place a new prediction (confirm)
            if make_predictions is True:
                self.ws_pool.submit(
                    PubsubTopic(
                        "predictions-user-v1",
                        user_id=self.twitch.twitch_login.get_user_id(),
                    )
                )

            for streamer in self.streamers:
                self.ws_pool.submit(
                    PubsubTopic("video-playback-by-id", streamer=streamer)
                )

                if streamer.settings.follow_raid is True:
                    self.ws_pool.submit(PubsubTopic("raid", streamer=streamer))

                if streamer.settings.make_predictions is True:
                    self.ws_pool.submit(
                        PubsubTopic("predictions-channel-v1", streamer=streamer)
                    )

            refresh_streamers = time.time()
            while self.running:
                time.sleep(random.uniform(20, 60))
                # Do an external control for WebSocket. Check if the thread is running
                # Check if is not None because maybe we have already created a new connection on array+1 and now index is None
                for index in range(0, len(self.ws_pool.ws)):
                    if (
                        self.ws_pool.ws[index].is_reconneting is False
                        and self.ws_pool.ws[index].elapsed_last_ping() > 10
                        and internet_connection_available() is True
                    ):
                        logger.info(
                            f"#{index} - The last PING was sent more than 10 minutes ago. Reconnecting to the WebSocket..."
                        )
                        WebSocketsPool.handle_reconnection(self.ws_pool.ws[index])

                if (
                    self.refresh_streamers is not None
                    and (time.time() - refresh_streamers) / 60 > self.refresh_streamers
                ):
                    refresh_streamers = time.time()
                    self.streamers = self.__reload_streamers(
                        streamers=streamers,
                        streamers_json=streamers_json,
                        followers=followers,
                    )
                    self.__save_original()

    @staticmethod
    def __remove_missing(streamers_name, streamers_dict, items, reason):
        for username in items:
            if username in streamers_name:
                logger.info(
                    f"Remove the streamer: {username}. {reason}",
                    extra={"emoji": ":wastebasket:"},
                )
                streamers_name.remove(username)
                del streamers_dict[username]
        return streamers_name, streamers_dict

    def __reload_streamers(
        self,
        streamers: list = [],
        blacklist: list = [],
        streamers_json: str = None,
        followers: bool = False,
    ):
        streamers_array = []
        Settings.streamers_settings = []

        # Use for remove duplicate with followers + keep order from array
        streamers_name: list = []
        # Use for prevent duplicate and easy access
        streamers_dict: dict = {}
        for streamer in self.streamers:
            if streamer.username not in blacklist:
                streamers_name.append(streamer.username)
                streamers_dict[streamer.username] = streamer

        # In the run.py a username can insert a "username" string or Streamer istance
        for streamer in streamers:
            username = (
                streamer.username
                if isinstance(streamer, Streamer)
                else streamer.lower().strip()
            )
            if username not in streamers_dict and username not in blacklist:
                streamers_name.append(username)
                streamers_dict[username] = (
                    streamer
                    if isinstance(streamer, Streamer) is True
                    else Streamer(streamer)
                )
        logger.info(
            f"Load {len(streamers)} streamers from: [STREAMERS]",
            extra={"emoji": ":clipboard:"},
        )

        # Steps.
        #   1. Read json file and iterate.
        #   2. If this user It's already in list just parse the json file and update the settings
        #   3. If this user It's not in list append the new Streamer
        if streamers_json is not None:
            json_streamers = Settings.read(streamers_json)
            usernames = []
            for streamer_dict in json_streamers:
                if username not in blacklist:
                    username = streamer_dict["username"]
                    usernames.append(username)
                    streamer = Streamer(
                        username,
                        settings=Settings.parse(
                            streamer_dict["settings"], StreamerSettings
                        ),
                    )
                    if username not in streamers_dict:
                        streamers_name.append(username)
                        streamers_dict[username] = streamer
                    else:
                        # Get settings form json
                        streamers_dict[username].settings = streamer.settings

            if self.sources_usernames["JSON_FILE"] != []:
                streamers_name, streamers_dict = self.__remove_missing(
                    streamers_name,
                    streamers_dict,
                    list(
                        filter(
                            lambda x: x not in usernames,
                            self.sources_usernames["JSON_FILE"],
                        )
                    ),
                    reason="Not present in JSON file",
                )
            self.sources_usernames["JSON_FILE"] = usernames

            logger.info(
                f"Load {len(json_streamers)} streamers from: [JSON FILE]",
                extra={"emoji": ":clipboard:"},
            )

        if followers is True:
            followers_array = self.twitch.get_followers()
            followers_array = list(
                filter(lambda x: x not in blacklist, followers_array)
            )

            for username in followers_array:
                if username not in streamers_dict:
                    streamers_dict[username] = Streamer(username.lower().strip())
                    streamers_name.append(username)

            if self.sources_usernames["FOLLOWERS"] != []:
                unfollowed = list(
                    filter(
                        lambda x: x not in followers_array,
                        self.sources_usernames["FOLLOWERS"],
                    )
                )
                streamers_name, streamers_dict = self.__remove_missing(
                    streamers_name,
                    streamers_dict,
                    unfollowed,
                    reason="User was unfollowed",
                )
            self.sources_usernames["FOLLOWERS"] = followers_array

            logger.info(
                f"Load {len(followers_array)} streamers from: [FOLLOWERS]",
                extra={"emoji": ":clipboard:"},
            )

        count = len(
            [
                username
                for username in streamers_dict
                if streamers_dict[username].init_processed is False
            ]
        )
        logger.info(
            f"Loading data for new {count} streamers. Please wait...",
            extra={"emoji": ":nerd_face:"},
        )
        for username in streamers_name:
            try:
                streamer = streamers_dict[username]
                Settings.append(streamer)  # Store in .json the original settings
                if (
                    streamer.init_processed is False
                ):  # We have already the channel_id and other values.
                    time.sleep(random.uniform(0.3, 0.7))
                    streamer.channel_id = self.twitch.get_channel_id(username)

                # Init all the missing settings with a default value
                streamer.settings = set_default_settings(
                    streamer.settings, Settings.streamer_settings
                )
                streamer.settings.bet = set_default_settings(
                    streamer.settings.bet, Settings.streamer_settings.bet
                )
                if streamer.settings.join_chat is True and streamer.irc_chat is None:
                    streamer.irc_chat = ThreadChat(
                        self.username,
                        self.twitch.twitch_login.get_auth_token(),
                        streamer.username,
                    )

                streamers_array.append(streamer)
            except StreamerDoesNotExistException:
                logger.info(
                    f"Streamer {username} does not exist",
                    extra={"emoji": ":cry:"},
                )

        if streamers_json is not None:
            Settings.write(streamers_json)

        # Populate the streamers with default values.
        # 1. Load channel points and auto-claim bonus
        # 2. Check if streamers is online
        # 3. Check if the user is a Streamer. In this case you can't do prediction
        for streamer in streamers_array:
            if streamer.init_processed is False:
                time.sleep(random.uniform(0.3, 0.7))
                self.twitch.load_channel_points_context(streamer)
                self.twitch.check_streamer_online(streamer)
                self.twitch.viewer_is_mod(streamer)
                if streamer.viewer_is_mod is True:
                    streamer.settings.make_predictions = False

                streamer.init_processed = True

        return streamers_array

    def __save_original(self):
        for streamer in self.streamers:
            if streamer.username not in self.base_points:
                self.base_points[streamer.username] = streamer.channel_points

    def end(self, signum, frame):
        logger.info("CTRL+C Detected! Please wait just a moment!")

        for streamer in self.streamers:
            if streamer.irc_chat is not None:
                streamer.leave_chat()
                if streamer.irc_chat.is_alive() is True:
                    streamer.irc_chat.join()

        self.running = self.twitch.running = False
        self.ws_pool.end()

        if self.minute_watcher_thread is not None:
            self.minute_watcher_thread.join()

        if self.sync_campaigns_thread is not None:
            self.sync_campaigns_thread.join()

        # Check if all the mutex are unlocked.
        # Prevent breaks of .json file
        for streamer in self.streamers:
            if streamer.mutex.locked():
                streamer.mutex.acquire()
                streamer.mutex.release()

        self.__print_report()

        sys.exit(0)

    def __print_report(self):
        print("\n")
        logger.info(
            f"Ending session: '{self.session_id}'", extra={"emoji": ":stop_sign:"}
        )
        if self.logs_file is not None:
            logger.info(
                f"Logs file: {self.logs_file}", extra={"emoji": ":page_facing_up:"}
            )
        logger.info(
            f"Duration {datetime.now() - self.start_datetime}",
            extra={"emoji": ":hourglass:"},
        )

        if self.events_predictions != {}:
            print("")
            for event_id in self.events_predictions:
                event = self.events_predictions[event_id]
                if (
                    event.bet_confirmed is True
                    and event.streamer.settings.make_predictions is True
                ):
                    logger.info(
                        f"{event.streamer.settings.bet}",
                        extra={"emoji": ":wrench:"},
                    )
                    if event.streamer.settings.bet.filter_condition is not None:
                        logger.info(
                            f"{event.streamer.settings.bet.filter_condition}",
                            extra={"emoji": ":pushpin:"},
                        )
                    logger.info(
                        f"{event.print_recap()}",
                        extra={"emoji": ":bar_chart:"},
                    )

        print("")
        for streamer in self.streamers:
            if streamer.history != {}:
                gained = streamer.channel_points - self.base_points[streamer.username]
                logger.info(
                    f"{repr(streamer)}, Total Points Gained (after farming - before farming): {_millify(gained)}",
                    extra={"emoji": ":robot:"},
                )
                logger.info(
                    f"{streamer.print_history()}",
                    extra={"emoji": ":moneybag:"},
                )
