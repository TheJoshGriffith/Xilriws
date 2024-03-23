from __future__ import annotations

import asyncio
import re

import httpx
import nodriver


class LoginException(Exception):
    pass


class PtcAuth:
    ACCESS_URL = "https://access.pokemon.com/"
    browser: nodriver.Browser | None = None
    tab: nodriver.Tab | None = None
    reese_cookie: str | None = None
    browser_task: asyncio.Task | None = None

    async def auth(self, username: str, password: str, full_url: str, proxy: str | None = None) -> str:
        if self.reese_cookie:
            return await self.serving_auth_the_old_fashioned_way(username, password, full_url=full_url, proxy=proxy)

        if self.browser_task:
            await self.browser_task
            return await self.auth(
                username, password, full_url, proxy
            )  # potentially bad if it didn't get a cookie or whatever

        self.browser_task = asyncio.create_task(self.browser_auth(username, password, full_url))
        code = await self.browser_task
        self.browser_task = None
        return code

    async def browser_auth(self, username: str, password: str, full_url: str) -> str:
        if not self.browser:
            self.browser = await nodriver.start(headless=True)

        js_future = asyncio.get_running_loop().create_future()

        async def js_check_handler(event: nodriver.cdp.network.ResponseReceived):
            url = event.response.url
            if not url.startswith("https://access.pokemon.com/"):
                return
            if not url.endswith("?d=access.pokemon.com"):
                return
            js_future.set_result(True)

        self.tab = await self.browser.get(url=full_url)
        self.tab.add_handler(nodriver.cdp.network.ResponseReceived, js_check_handler)

        html = await self.tab.get_content()
        if "Log in" not in html:
            try:
                await asyncio.wait_for(js_future, timeout=10)
            except asyncio.TimeoutError:
                raise LoginException("Timeout on JS challenge")

            await self.tab.reload()

        self.tab.handlers.clear()

        raw_cookies = await self.tab.evaluate("document.cookie")
        cookies = raw_cookies.split("; ")
        for cookie in cookies:
            split_cookie = cookie.split("=")
            if split_cookie[0] == "reese84":
                cookie_value = "=".join(split_cookie[1:])
                self.reese_cookie = cookie_value

        accept_input = await self.tab.wait_for("input#accept")

        js_email = f'document.querySelector("input#email").value="{username}"'
        js_pass = f'document.querySelector("input#password").value="{password}"'

        await self.tab.evaluate(js_email + ";" + js_pass)

        pokemongo_url_future = asyncio.get_running_loop().create_future()

        async def send_handler(event: nodriver.cdp.network.RequestWillBeSent):
            url = event.request.url
            if url.startswith("pokemongo://"):
                pokemongo_url_future.set_result(url)

        self.tab.add_handler(nodriver.cdp.network.RequestWillBeSent, send_handler)

        await accept_input.click()

        await self.tab.wait_for("html")
        await self.tab.update_target()

        if self.tab.target.url.startswith("https://access.pokemon.com/consent"):
            consent_accept = await self.tab.wait_for("input#accept")
            if not consent_accept:
                raise LoginException("no consent button")
            await consent_accept.click()

        try:
            pokemongo_url = await asyncio.wait_for(pokemongo_url_future, timeout=15)
        except asyncio.TimeoutError:
            print("Timeout error!")
            raise LoginException("Timeout while waiting for browser to finish")

        self.tab.handlers.clear()

        login_code = self.__extract_login_code(pokemongo_url)
        if not login_code:
            raise LoginException("no login code found")

        return login_code

    async def serving_auth_the_old_fashioned_way(
        self, username: str, password: str, full_url: str, proxy: str | None = None
    ) -> str:
        if not self.reese_cookie:
            raise LoginException("this error does not happen")

        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}

        async with httpx.AsyncClient(
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-us",
                "Connection": "keep-alive",
                "Accept-Encoding": "gzip, deflate, br",
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/14.0.2 Mobile/15E148 Safari/604.1",
            },
            follow_redirects=True,
            verify=False,
            timeout=10,
            proxies=proxies,
            cookies={"reese84": self.reese_cookie},
        ) as client:
            resp = await client.get(full_url)

            if resp.status_code == 403:
                self.reese_cookie = None
                print("cookie expired. opening the browser")
                return await self.auth(username, password, full_url, proxy)  # bad

            if resp.status_code != 200:
                raise LoginException(f"OAUTH: {resp.status_code} but expected 200")

            csrf, challenge = self.__extract_csrf_and_challenge(resp.text)

            login_resp = await client.post(
                self.ACCESS_URL + "login",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"_csrf": csrf, "challenge": challenge, "email": username, "password": password},
            )

            if resp.status_code == 403:
                self.reese_cookie = None
                print("cookie expired. opening the browser")
                return await self.auth(username, password, full_url, proxy)  # bad

            if login_resp.status_code != 200:
                raise LoginException(f"LOGIN: {login_resp.status_code} but expected 200")

            login_code = self.__extract_login_code(login_resp.text)

            if not login_code:
                raise LoginException("login failed")

            return login_code

    def __extract_login_code(self, html) -> str | None:
        matches = re.search(r"pokemongo://state=(.*?)(?:,code=(.*?))?(?='|$)", html)

        if matches and len(matches.groups()) == 2:
            return matches.group(2)

    def __extract_csrf_and_challenge(self, html: str) -> tuple[str, str]:
        csrf_regex = re.compile(r'name="_csrf" value="(.*?)">')
        challenge_regex = re.compile(r'name="challenge" value="(.*?)">')

        csrf_matches = csrf_regex.search(html)
        challenge_matches = challenge_regex.search(html)

        if csrf_matches and challenge_matches:
            return csrf_matches.group(1), challenge_matches.group(1)

        raise LoginException(f"Couldn't find CSRF or challenge in Auth response")