"""Scraper for http://whoscored.com."""
import itertools
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

import pandas as pd
from lxml import html
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

from ._common import (
    BaseSeleniumReader,
    make_game_id,
    season_code,
    standardize_colnames,
)
from ._config import DATA_DIR, NOCACHE, NOSTORE, TEAMNAME_REPLACEMENTS, logger

WHOSCORED_DATADIR = DATA_DIR / "WhoScored"
WHOSCORED_URL = "https://www.whoscored.com"


class WhoScored(BaseSeleniumReader):
    """Provides pd.DataFrames from data available at http://whoscored.com.

    Data will be downloaded as necessary and cached locally in
    ``~/soccerdata/data/WhoScored``.

    Parameters
    ----------
    leagues : string or iterable, optional
        IDs of Leagues to include.
    seasons : string, int or list, optional
        Seasons to include. Supports multiple formats.
        Examples: '16-17'; 2016; '2016-17'; [14, 15, 16]
    proxy : 'tor' or or dict or list(dict) or callable, optional
        Use a proxy to hide your IP address. Valid options are:
            - "tor": Uses the Tor network. Tor should be running in
              the background on port 9050.
            - dict: A dictionary with the proxy to use. The dict should be
              a mapping of supported protocols to proxy addresses. For example::

                  {
                      'http': 'http://10.10.1.10:3128',
                      'https': 'http://10.10.1.10:1080',
                  }

            - list(dict): A list of proxies to choose from. A different proxy will
              be selected from this list after failed requests, allowing rotating
              proxies.
            - callable: A function that returns a valid proxy. This function will
              be called after failed requests, allowing rotating proxies.
    no_cache : bool
        If True, will not use cached data.
    no_store : bool
        If True, will not store downloaded data.
    data_dir : Path
        Path to directory where data will be cached.
    path_to_browser : Path, optional
        Path to the Chrome executable.
    """

    def __init__(
        self,
        leagues: Optional[Union[str, List[str]]] = None,
        seasons: Optional[Union[str, int, Iterable[Union[str, int]]]] = None,
        proxy: Optional[
            Union[str, Dict[str, str], List[Dict[str, str]], Callable[[], Dict[str, str]]]
        ] = None,
        no_cache: bool = NOCACHE,
        no_store: bool = NOSTORE,
        data_dir: Path = WHOSCORED_DATADIR,
        path_to_browser: Optional[Path] = None,
    ):
        """Initialize the WhoScored reader."""
        super().__init__(
            leagues=leagues,
            proxy=proxy,
            no_cache=no_cache,
            no_store=no_store,
            data_dir=data_dir,
            path_to_browser=path_to_browser,
        )
        self.seasons = seasons  # type: ignore
        self.rate_limit = 5
        self.max_delay = 5
        if not self.no_store:
            (self.data_dir / "seasons").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "matches").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "previews").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "events").mkdir(parents=True, exist_ok=True)

    def read_leagues(self) -> pd.DataFrame:
        """Retrieve the selected leagues from the datasource.

        Returns
        -------
        pd.DataFrame
        """
        url = WHOSCORED_URL
        filepath = self.data_dir / "tiers.json"
        reader = self.get(url, filepath, var="allRegions")

        data = json.load(reader)

        leagues = []
        for region in data:
            for league in region["tournaments"]:
                leagues.append(
                    {
                        "region_id": region["id"],
                        "region": region["name"],
                        "league_id": league["id"],
                        "league": league["name"],
                        "url": league["url"],
                    }
                )

        df = (
            pd.DataFrame(leagues)
            .assign(league=lambda x: x.region + " - " + x.league)
            .pipe(self._translate_league)
            .set_index("league")
            .loc[self._selected_leagues.keys()]
            .sort_index()
        )
        return df

    def read_seasons(self) -> pd.DataFrame:
        """Retrieve the selected seasons for the selected leagues.

        Returns
        -------
        pd.DataFrame
        """
        df_leagues = self.read_leagues()

        seasons = []
        for lkey, league in df_leagues.iterrows():
            url = WHOSCORED_URL + league.url
            filemask = "seasons/{}.html"
            filepath = self.data_dir / filemask.format(lkey)
            reader = self.get(url, filepath, var=None)

            # extract team links
            tree = html.parse(reader)
            for node in tree.xpath("//select[contains(@id,'seasons')]/option"):
                # extract team IDs from links
                seasons.append(
                    {
                        "url": node.get("value"),
                        "league": lkey,
                        "league_id": league.league_id,
                        "season": season_code(node.text),
                    }
                )

        df = (
            pd.DataFrame(seasons)
            .set_index(["league", "season"])
            .sort_index()
            .loc[itertools.product(self.leagues, self.seasons)]
        )
        return df

    def _parse_season_stages(self) -> List[Dict]:
        match_selector = (
            "//div[contains(@id,'tournament-fixture')]//div[contains(@class,'divtable-row')]"
        )
        time.sleep(5 + random.random() * 5)
        WebDriverWait(self._driver, 30, poll_frequency=1).until(
            ec.presence_of_element_located((By.XPATH, match_selector))
        )
        stages = []
        node_stages_selector = "//select[contains(@id,'stages')]/option"
        node_stages = self._driver.find_elements(By.XPATH, node_stages_selector)
        for stage in node_stages:
            stages.append({"url": stage.get_attribute("value"), "name": stage.text})
        return stages

    def _parse_schedule_page(self) -> Tuple[List[Dict], Optional[WebElement]]:
        match_selector = (
            "//div[contains(@id,'tournament-fixture')]//div[contains(@class,'divtable-row')]"
        )
        time.sleep(5 + random.random() * 5)
        WebDriverWait(self._driver, 30, poll_frequency=1).until(
            ec.presence_of_element_located((By.XPATH, match_selector))
        )
        date_str = "Monday, Jan 1 2021"
        schedule_page = []
        for node in self._driver.find_elements(By.XPATH, match_selector):
            if node.get_attribute("data-id"):
                time_str = node.find_element(By.XPATH, "./div[contains(@class,'time')]").text
                schedule_page.append(
                    {
                        "date": datetime.strptime(f"{date_str} {time_str}", "%A, %b %d %Y %H:%M"),
                        "home_team": node.find_element(
                            By.XPATH, "./div[contains(@class,'team home')]//a"
                        ).text,
                        "away_team": node.find_element(
                            By.XPATH, "./div[contains(@class,'team away')]//a"
                        ).text,
                        # fmt: off
                        "game_id": int(
                            re.search(
                                r"Matches/(\d+)/",
                                node.find_element(
                                    By.XPATH,
                                    "./div[contains(@class,'result')]//a"
                                ).get_attribute("href")).group(1)  # type: ignore
                        ),
                        # fmt: on
                        "url": node.find_element(
                            By.XPATH, "./div[contains(@class,'result')]//a"
                        ).get_attribute("href"),
                    }
                )
            else:
                date_str = node.find_element(
                    By.XPATH, "./div[contains(@class,'divtable-header')]"
                ).text
                logger.info("Scraping game schedule for %s", date_str)

        try:
            next_page = self._driver.find_element(
                By.XPATH,
                "//div[contains(@id,'date-controller')]/a[contains(@class,'previous') and not(contains(@class, 'is-disabled'))]",  # noqa: E501
            )
        except NoSuchElementException:
            next_page = None
        return schedule_page, next_page

    def _parse_schedule(self, stage: Optional[str] = None) -> List[Dict]:
        schedule = []
        # Parse first page
        page_schedule, next_page = self._parse_schedule_page()
        schedule.extend(page_schedule)
        # Go to next page
        while next_page is not None:
            try:
                next_page.click()
                logger.debug("Next page")
            except ElementClickInterceptedException:
                self._handle_banner()
            # Parse next page
            page_schedule, next_page = self._parse_schedule_page()
            schedule.extend(page_schedule)
        schedule = [dict(item, stage=stage) for item in schedule]
        return schedule

    def read_schedule(self, force_cache: bool = False) -> pd.DataFrame:
        """Retrieve the game schedule for the selected leagues and seasons.

        Parameters
        ----------
        force_cache : bool
             By default no cached data is used for the current season.
             If True, will force the use of cached data anyway.

        Returns
        -------
        pd.DataFrame
        """
        df_seasons = self.read_seasons()
        filemask = "matches/{}_{}.csv"

        all_schedules = []
        for (lkey, skey), season in df_seasons.iterrows():
            filepath = self.data_dir / filemask.format(lkey, skey)
            url = WHOSCORED_URL + season.url
            current_season = not self._is_complete(lkey, skey)
            schedule = []
            if current_season and not force_cache or (not filepath.exists()) or self.no_cache:
                time.sleep(random.random() * 5)
                self._driver.get(url)
                stages = self._parse_season_stages()
                if len(stages) > 0:
                    for stage in stages:
                        url = WHOSCORED_URL + stage["url"].replace("Show", "Fixtures")
                        self._driver.get(url)
                        try:
                            self._driver.find_element(By.XPATH, "//div[@id='tournament-fixture']")
                        except NoSuchElementException:
                            # Tournaments sometimes do not have a fixtures page,
                            # the summary page has to be used instead
                            url = WHOSCORED_URL + stage["url"]
                            self._driver.get(url)
                        logger.info("Scraping game schedule with stage=%s from %s", stage, url)
                        schedule.extend(self._parse_schedule(stage=stage["name"]))
                else:
                    url = self._driver.find_element(
                        By.XPATH, "//a[text()='Fixtures']"
                    ).get_attribute("href")
                    self._driver.get(url)
                    try:
                        self._driver.find_element(By.XPATH, "//div[@id='tournament-fixture']")
                    except NoSuchElementException:
                        # Tournaments sometimes do not have a fixtures page,
                        # the summary page has to be used instead
                        url = self._driver.find_element(
                            By.XPATH, "//a[text()='Summary']"
                        ).get_attribute("href")
                        self._driver.get(url)
                    logger.info("Scraping game schedule from %s", url)
                    schedule.extend(self._parse_schedule())
                df_schedule = pd.DataFrame(schedule).assign(league=lkey, season=skey)
                if not self.no_store:
                    df_schedule.to_csv(filepath, index=False)
            else:
                logger.info("Retrieving game schedule of %s - %s from the cache", lkey, skey)
                df_schedule = pd.read_csv(filepath)
            all_schedules.append(df_schedule)

        df = (
            pd.concat(all_schedules)
            .drop_duplicates()
            .replace(
                {
                    "home_team": TEAMNAME_REPLACEMENTS,
                    "away_team": TEAMNAME_REPLACEMENTS,
                }
            )
            .assign(date=lambda x: pd.to_datetime(x["date"]))
            .assign(game=lambda df: df.apply(make_game_id, axis=1))
            .set_index(["league", "season", "game"])
            .sort_index()
        )
        return df

    def _read_game_info(self, game_id: int) -> Dict:
        """Return game info available in the header."""
        urlmask = WHOSCORED_URL + "/Matches/{}"
        url = urlmask.format(game_id)
        data = {}
        self._driver.get(url)
        # league and season
        breadcrumb = self._driver.find_elements(
            By.XPATH, "//div[@id='breadcrumb-nav']/*[not(contains(@class, 'separator'))]"
        )
        country = breadcrumb[0].text
        league, season = breadcrumb[1].text.split(" - ")
        data["league"] = {v: k for k, v in self._all_leagues().items()}[f"{country} - {league}"]
        data["season"] = season_code(season)
        # match header
        match_header = self._driver.find_element(By.XPATH, "//div[@id='match-header']")
        score_info = match_header.find_element(By.XPATH, ".//div[@class='teams-score-info']")
        data["home_team"] = score_info.find_element(
            By.XPATH, "./span[contains(@class,'home team')]"
        ).text
        data["result"] = score_info.find_element(
            By.XPATH, "./span[contains(@class,'result')]"
        ).text
        data["away_team"] = score_info.find_element(
            By.XPATH, "./span[contains(@class,'away team')]"
        ).text
        info_blocks = match_header.find_elements(By.XPATH, ".//div[@class='info-block cleared']")
        for block in info_blocks:
            for desc_list in block.find_elements(By.TAG_NAME, "dl"):
                for desc_def in desc_list.find_elements(By.TAG_NAME, "dt"):
                    desc_val = desc_def.find_element(By.XPATH, "./following-sibling::dd")
                    data[desc_def.text] = desc_val.text

        return data

    def read_missing_players(
        self, match_id: Optional[Union[int, List[int]]] = None, force_cache: bool = False
    ) -> pd.DataFrame:
        """Retrieve a list of injured and suspended players ahead of each game.

        Parameters
        ----------
        match_id : int or list of int, optional
            Retrieve the missing players for a specific game.
        force_cache : bool
            By default no cached data is used to scrapre the list of available
            games for the current season. If True, will force the use of
            cached data anyway.

        Raises
        ------
        ValueError
            If the given match_id could not be found in the selected seasons.

        Returns
        -------
        pd.DataFrame
        """
        urlmask = WHOSCORED_URL + "/Matches/{}/Preview"
        filemask = "WhoScored/previews/{}_{}/{}.html"

        df_schedule = self.read_schedule(force_cache).reset_index()
        if match_id is not None:
            iterator = df_schedule[
                df_schedule.game_id.isin([match_id] if isinstance(match_id, int) else match_id)
            ]
            if len(iterator) == 0:
                raise ValueError("No games found with the given IDs in the selected seasons.")
        else:
            iterator = df_schedule.sample(frac=1)

        match_sheets = []
        for i, (_, game) in enumerate(iterator.iterrows()):
            url = urlmask.format(game.game_id)
            filepath = DATA_DIR / filemask.format(game["league"], game["season"], game["game_id"])

            logger.info(
                "[%s/%s] Retrieving game with id=%s", i + 1, len(iterator), game["game_id"]
            )
            reader = self.get(url, filepath, var=None)

            # extract missing players
            tree = html.parse(reader)
            for node in tree.xpath("//div[@id='missing-players']/div[2]/table/tbody/tr"):
                # extract team IDs from links
                match_sheets.append(
                    {
                        "league": game["league"],
                        "season": game["season"],
                        "game": game["game"],
                        "game_id": game["game_id"],
                        "team": game["home_team"],
                        "player": node.xpath("./td[contains(@class,'pn')]/a")[0].text,
                        "player_id": int(
                            node.xpath("./td[contains(@class,'pn')]/a")[0]
                            .get("href")
                            .split("/")[2]
                        ),
                        "reason": node.xpath("./td[contains(@class,'reason')]/span")[0].get(
                            "title"
                        ),
                        "status": node.xpath("./td[contains(@class,'confirmed')]")[0].text,
                    }
                )
            for node in tree.xpath("//div[@id='missing-players']/div[3]/table/tbody/tr"):
                # extract team IDs from links
                match_sheets.append(
                    {
                        "league": game["league"],
                        "season": game["season"],
                        "game": game["game"],
                        "game_id": game["game_id"],
                        "team": game["away_team"],
                        "player": node.xpath("./td[contains(@class,'pn')]/a")[0].text,
                        "player_id": int(
                            node.xpath("./td[contains(@class,'pn')]/a")[0]
                            .get("href")
                            .split("/")[2]
                        ),
                        "reason": node.xpath("./td[contains(@class,'reason')]/span")[0].get(
                            "title"
                        ),
                        "status": node.xpath("./td[contains(@class,'confirmed')]")[0].text,
                    }
                )
        df = (
            pd.DataFrame(match_sheets)
            .set_index(["league", "season", "game", "team", "player"])
            .sort_index()
        )
        return df

    def read_events(  # noqa: C901
        self,
        match_id: Optional[Union[int, List[int]]] = None,
        force_cache: bool = False,
        live: bool = False,
        output_fmt: Optional[str] = "events",
    ) -> Optional[Union[pd.DataFrame, Dict[int, List], "OptaLoader"]]:  # type: ignore  # noqa: F821
        """Retrieve the the event data for each game in the selected leagues and seasons.

        Parameters
        ----------
        match_id : int or list of int, optional
            Retrieve the event stream for a specific game.
        force_cache : bool
            By default no cached data is used to scrape the list of available
            games for the current season. If True, will force the use of
            cached data anyway.
        live : bool
            If True, will not return a cached copy of the event data. This is
            usefull to scrape live data.
        output_fmt : str, default: 'events'
            The output format of the returned data. Possible values are:
                - 'events' (default): Returns a dataframe with all events.
                - 'raw': Returns the original unformatted WhoScored JSON.
                - 'spadl': Returns a dataframe with the SPADL representation
                  of the original events.
                  See https://socceraction.readthedocs.io/en/latest/documentation/SPADL.html#spadl
                - 'atomic-spadl': Returns a dataframe with the Atomic-SPADL representation
                  of the original events.
                  See https://socceraction.readthedocs.io/en/latest/documentation/SPADL.html#atomic-spadl
                - 'loader': Returns a socceraction.data.opta.OptaLoader
                  instance, which can be used to retrieve the actual data.
                  See https://socceraction.readthedocs.io/en/latest/modules/generated/socceraction.data.opta.OptaLoader.html#socceraction.data.opta.OptaLoader  # noqa: E501
                - None: Doesn't return any data. This is useful to just cache
                  the data without storing the events in memory.

        Raises
        ------
        ValueError
            If the given match_id could not be found in the selected seasons.
        ImportError
            If the requested output format is 'spadl', 'atomic-spadl' or
            'loader' but the socceraction package is not installed.

        Returns
        -------
        See the description of the ``output_fmt`` parameter.
        """
        output_fmt = output_fmt.lower() if output_fmt is not None else None
        if output_fmt in ["loader", "spadl", "atomic-spadl"]:
            if self.no_store:
                raise ValueError(
                    f"The '{output_fmt}' output format is not supported "
                    "when using the 'no_store' option."
                )
            try:
                from socceraction.atomic.spadl import convert_to_atomic
                from socceraction.data.opta import OptaLoader
                from socceraction.data.opta.loader import _eventtypesdf
                from socceraction.data.opta.parsers import WhoScoredParser
                from socceraction.spadl.opta import convert_to_actions

                if output_fmt == "loader":
                    import socceraction
                    from packaging import version

                    if version.parse(socceraction.__version__) < version.parse("1.2.3"):
                        raise ImportError(
                            "The 'loader' output format requires socceraction >= 1.2.3"
                        )
            except ImportError:
                raise ImportError(
                    "The socceraction package is required to use the 'spadl' "
                    "or 'atomic-spadl' output format. "
                    "Please install it with `pip install socceraction`."
                )
        urlmask = WHOSCORED_URL + "/Matches/{}/Live"
        filemask = "events/{}_{}/{}.json"

        df_schedule = self.read_schedule(force_cache).reset_index()
        if match_id is not None:
            iterator = df_schedule[
                df_schedule.game_id.isin([match_id] if isinstance(match_id, int) else match_id)
            ]
            if len(iterator) == 0:
                raise ValueError("No games found with the given IDs in the selected seasons.")
        else:
            iterator = df_schedule.sample(frac=1)

        events = {}
        player_names = {}
        team_names = {}
        for i, (_, game) in enumerate(iterator.iterrows()):
            url = urlmask.format(game["game_id"])
            # get league and season
            logger.info(
                "[%s/%s] Retrieving game with id=%s", i + 1, len(iterator), game["game_id"]
            )
            filepath = self.data_dir / filemask.format(
                game["league"], game["season"], game["game_id"]
            )

            reader = self.get(
                url,
                filepath,
                var="requirejs.s.contexts._.config.config.params.args.matchCentreData",
                no_cache=live,
            )
            json_data = json.load(reader)
            if json_data is not None:
                player_names.update(
                    {int(k): v for k, v in json_data["playerIdNameDictionary"].items()}
                )
                team_names.update(
                    {
                        int(json_data[side]["teamId"]): json_data[side]["name"]
                        for side in ["home", "away"]
                    }
                )
                if "events" in json_data:
                    game_events = json_data["events"]
                    if output_fmt == "events":
                        df_events = pd.DataFrame(game_events)
                        df_events["game"] = game["game"]
                        df_events["league"] = game["league"]
                        df_events["season"] = game["season"]
                        df_events["game_id"] = game["game_id"]
                        events[game["game_id"]] = df_events
                    elif output_fmt == "raw":
                        events[game["game_id"]] = game_events
                    elif output_fmt in ["spadl", "atomic-spadl"]:
                        parser = WhoScoredParser(
                            str(filepath),
                            competition_id=game["league"],
                            season_id=game["season"],
                            game_id=game["game_id"],
                        )
                        df_events = (
                            pd.DataFrame.from_dict(parser.extract_events(), orient="index")
                            .merge(_eventtypesdf, on="type_id", how="left")
                            .reset_index(drop=True)
                        )
                        df_actions = convert_to_actions(
                            df_events, home_team_id=int(json_data["home"]["teamId"])
                        )
                        if output_fmt == "spadl":
                            events[game["game_id"]] = df_actions
                        else:
                            events[game["game_id"]] = convert_to_atomic(df_actions)

            else:
                logger.warning("No events found for game %s", game["game_id"])

        if output_fmt is None:
            return None

        if output_fmt == "raw":
            return events

        if output_fmt == "loader":
            return OptaLoader(
                root=self.data_dir,
                parser="whoscored",
                feeds={"whoscored": "events/{competition_id}_{season_id}/{game_id}.json"},
            )

        df = (
            pd.concat(events.values())
            .pipe(standardize_colnames)
            .assign(
                player=lambda x: x.player_id.replace(player_names),
                team=lambda x: x.team_id.replace(team_names).replace(TEAMNAME_REPLACEMENTS),
            )
        )

        if output_fmt == "events":
            df = df.set_index(["league", "season", "game", "id"]).sort_index()
            df["outcome_type"] = df["outcome_type"].apply(lambda x: x.get("displayName"))
            df["type"] = df["type"].apply(lambda x: x.get("displayName"))
            df["period"] = df["period"].apply(lambda x: x.get("displayName"))
            df = df[
                [
                    "period",
                    "minute",
                    "expanded_minute",
                    "type",
                    "outcome_type",
                    "team",
                    "player",
                    "qualifiers",
                    "x",
                    "y",
                    "end_x",
                    "end_y",
                    "goal_mouth_y",
                    "goal_mouth_z",
                    "is_touch",
                    "is_shot",
                    "is_goal",
                    "related_event_id",
                    "related_player_id",
                    "blocked_x",
                    "blocked_y",
                    "card_type",
                    "game_id",
                    "team_id",
                    "player_id",
                ]
            ]

        return df

    def _handle_banner(self) -> None:
        try:
            # self._driver.get(WHOSCORED_URL)
            time.sleep(2)
            self._driver.find_element(By.XPATH, "//button[contains(text(), 'AGREE')]").click()
            time.sleep(2)
        except NoSuchElementException:
            with open("/tmp/error.html", "w") as f:
                f.write(self._driver.page_source)
            raise ElementClickInterceptedException()
