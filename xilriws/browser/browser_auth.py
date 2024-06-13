from __future__ import annotations

import asyncio
import re

import nodriver
from loguru import logger

from xilriws.constants import ACCESS_URL
from xilriws.debug import IS_DEBUG
from xilriws.extension_comm import FINISH_PROXY, FINISH_COOKIE_PURGE
from xilriws.ptc_auth import LoginException
from xilriws.reese_cookie import ReeseCookie
from .browser import Browser, ProxyException
from xilriws.ptc import ptc_utils


logger = logger.bind(name="Browser")


class BrowserAuth(Browser):
    first_run = True

    async def get_reese_cookie(self, proxy_changed: bool) -> ReeseCookie | None:
        proxy = self.proxies.next_proxy

        try:
            await self.start_browser()
        except Exception:
            return None

        try:
            js_future, js_check_handler = await self.get_js_check_handler(ACCESS_URL)
            cookie_future = await self.ext_comm.add_listener(FINISH_COOKIE_PURGE)

            await self.new_tab()
            if proxy_changed:
                await self.change_proxy()

            if not self.first_run and cookie_future and not cookie_future.done():
                try:
                    await asyncio.wait_for(cookie_future, 2)
                except asyncio.TimeoutError:
                    logger.info("Didn't get confirmation that cookies were cleared, continuing anyway")

            self.first_run = False

            if self.last_cookies:
                await self.browser.cookies.set_all(self.last_cookies)

            # if IS_DEBUG:
            #     await self.log_ip()

            self.tab.add_handler(nodriver.cdp.network.ResponseReceived, js_check_handler)
            logger.info("Opening PTC")
            await self.tab.get(url=ACCESS_URL + "login")

            html = await self.tab.get_content()
            if "neterror" in html.lower():
                raise ProxyException(f"Page couldn't be reached (Proxy: {proxy.url})")

            imp_code, imp_reason = ptc_utils.get_imperva_error_code(html)
            if imp_code not in ("15", "?"):
                proxy.rate_limited()
                raise LoginException(f"Error code {imp_code} ({imp_reason}) with (Proxy: {proxy.url})")
            else:
                logger.info("Successfully got error 15 page")
                if not js_future.done():
                    try:
                        logger.info("Waiting for JS check")
                        await asyncio.wait_for(js_future, timeout=20)
                        self.tab.handlers.clear()
                        logger.info("JS check done. reloading")
                    except asyncio.TimeoutError:
                        raise LoginException("Timeout on JS challenge")
                else:
                    logger.debug("JS check already done, continuing")
                await self.tab.reload()
                new_html = await self.tab.get_content()
                if "log in" not in new_html.lower():
                    logger.debug(new_html)
                    proxy.rate_limited()
                    imp_code, imp_reason = ptc_utils.get_imperva_error_code(new_html)
                    raise LoginException(f"Didn't pass JS check. Code {imp_code} ({imp_reason})")

            logger.info("Getting cookies from browser")
            all_cookies = await self.get_cookies()

            self.consecutive_failures = 0
            return ReeseCookie(all_cookies, proxy)
        except LoginException as e:
            logger.error(f"{str(e)} while getting cookie")
            self.consecutive_failures += 1
            return None
        except ProxyException as e:
            proxy.invalidate()
            logger.error(f"{str(e)} while getting cookie")
            self.stop_browser()
            return None
        except Exception as e:
            logger.exception("Exception in browser", e)

        logger.error("Error while getting cookie from browser, it will be restarted next time")
        self.consecutive_failures += 1
        self.stop_browser()
        return None
