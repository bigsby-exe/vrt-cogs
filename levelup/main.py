import asyncio
import logging
import multiprocessing as mp
import typing as t
from time import perf_counter

import orjson
from pydantic import ValidationError
from redbot.core import commands
from redbot.core.bot import Red
from redbot.core.data_manager import bundled_data_path, cog_data_path
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_list

from .abc import CompositeMetaClass
from .commands import Commands
from .commands.user import view_profile_context
from .common.models import DB, run_migrations
from .dashboard.integration import DashboardIntegration
from .generator import api
from .generator.trustytenor.converter import TenorAPI
from .listeners import Listeners
from .shared import SharedFunctions

log = logging.getLogger("red.vrt.levelup")
_ = Translator("LevelUp", __file__)
RequestType = t.Literal["discord_deleted_user", "owner", "user", "user_strict"]

# Generate translations
# redgettext -D -r levelup/ --command-docstring


@cog_i18n(_)
class LevelUp(
    Commands,
    SharedFunctions,
    DashboardIntegration,
    Listeners,
    commands.Cog,
    metaclass=CompositeMetaClass,
):
    """
    Your friendly neighborhood leveling system

    Earn experience by chatting in text and voice channels, compare levels with your friends, customize your profile and view various leaderboards!
    """

    __author__ = "[vertyco](https://github.com/vertyco/vrt-cogs)"
    __version__ = "4.0.0"
    __contributors__ = [
        "[aikaterna](https://github.com/aikaterna/aikaterna-cogs)",
        "[AAA3A](https://github.com/AAA3A-AAA3A/AAA3A-cogs)",
    ]

    def __init__(self, bot: Red, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot: Red = bot

        # Cache
        self.db: DB = DB()
        self.lastmsg: t.Dict[int, t.Dict[int, float]] = {}  # GuildID: {UserID: LastMessageTime}
        self.in_voice: t.Dict[int, t.Dict[int, float]] = {}  # GuildID: {UserID: TimeJoined}
        self.profile_cache: t.Dict[int, t.Dict[int, t.Tuple[str, bytes]]] = {}  # GuildID: {UserID: (last_used, bytes)}

        # Root Paths
        self.cog_path = cog_data_path(self)
        self.bundled_path = bundled_data_path(self)
        # Settings Files
        self.settings_file = self.cog_path / "LevelUp.json"
        self.old_settings_file = self.cog_path / "settings.json"
        # Custom Paths
        self.custom_fonts = self.cog_path / "fonts"
        self.custom_backgrounds = self.cog_path / "backgrounds"
        # Bundled Paths
        self.stock = self.bundled_path / "stock"
        self.fonts = self.bundled_path / "fonts"
        self.backgrounds = self.bundled_path / "backgrounds"

        # Save State
        self.saving = False
        self.last_save: float = perf_counter()

        # Tenor API
        self.tenor: TenorAPI = None

        # Imgen API
        self.api_proc: t.Union[asyncio.subprocess.Process, mp.Process] = None

    async def cog_load(self) -> None:
        self.bot.tree.add_command(view_profile_context)
        asyncio.create_task(self.initialize())

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(view_profile_context)
        if self.api_proc:
            api.kill(self.api_proc)

    async def initialize(self) -> None:
        await self.bot.wait_until_red_ready()
        if self.settings_file.exists():
            log.info("Loading config")
            try:
                self.db = await asyncio.to_thread(DB.from_file, self.settings_file)
            except ValidationError as e:
                log.error("Failed to load config", exc_info=e)
                return
        elif self.old_settings_file.exists():
            raw_settings = self.old_settings_file.read_text()
            settings = orjson.loads(raw_settings)
            if settings:
                log.warning("Migrating old settings.json")
                try:
                    self.db = await asyncio.to_thread(run_migrations, settings)
                    log.warning("Migration complete!")
                    self.save()
                except Exception as e:
                    log.error("Failed to migrate old settings.json", exc_info=e)
                    return

        log.info("Initializing voice states")
        voice_initialized = await self.initialize_voice_states()
        log.info(f"Config loaded, initialized {voice_initialized} voice states")

        self.custom_fonts.mkdir(exist_ok=True)
        self.custom_backgrounds.mkdir(exist_ok=True)
        logging.getLogger("PIL").setLevel(logging.WARNING)
        await self.load_tenor()
        if self.db.internal_api_port and not self.db.external_api_url:
            await self.start_api()

    async def start_api(self) -> bool:
        try:
            log_dir = self.cog_path / "APILogs"
            log_dir.mkdir(exist_ok=True, parents=True)
            self.api_proc = await api.run(log_dir=log_dir)
            log.debug(f"API Process started: {self.api_proc}")
            return True
        except Exception as e:
            log.error("Failed to start internal API", exc_info=e)
            return False

    async def load_tenor(self) -> None:
        tokens = await self.bot.get_shared_api_tokens("tenor")
        if "api_key" in tokens:
            log.debug("Tenor API token loaded")
            self.tenor = TenorAPI(tokens["api_key"], str(self.bot.user))

    async def on_red_api_tokens_update(self, service_name: str, api_tokens: t.Dict[str, str]) -> None:
        if service_name != "tenor":
            return
        if "api_key" in api_tokens:
            if self.tenor is not None:
                self.tenor._token = api_tokens["api_key"]
                return
            log.debug("Tenor API token updated")
            self.tenor = TenorAPI(api_tokens["api_key"], str(self.bot.user))

    def save(self) -> None:
        async def _save():
            if self.saving:
                log.debug("Already saving")
                return
            try:
                self.saving = True
                await asyncio.to_thread(self.db.to_file, self.settings_file)
                self.last_save = perf_counter()
                log.debug("Config saved")
            except Exception as e:
                log.error("Failed to save config", exc_info=e)
            finally:
                self.saving = False

        asyncio.create_task(_save())

    def format_help_for_context(self, ctx):
        helpcmd = super().format_help_for_context(ctx)
        info = (
            f"{helpcmd}\n"
            f"Cog Version: {self.__version__}\n"
            f"Author: {self.__author__}\n"
            f"Contributors: {humanize_list(self.__contributors__)}\n"
        )
        return info

    async def red_delete_data_for_user(self, *, requester: RequestType, user_id: int):
        return

    async def red_get_data_for_user(self, *, user_id: int):
        return
