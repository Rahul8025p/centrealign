import os
import time
import uuid

class BrowserTool:
    """
    Playwright-based Browser Automation Tool.
    Enables the AI Task Worker to control a real web browser:
    navigate, search, inspect DOM elements, type into forms, click buttons,
    and capture visual screenshots as evidence.
    """
    def __init__(self, headless: bool = True, screenshots_dir: str = None):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.page = None
        self.is_active = False

        if screenshots_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self.screenshots_dir = os.path.join(base_dir, "app", "static", "screenshots")
        else:
            self.screenshots_dir = screenshots_dir

        os.makedirs(self.screenshots_dir, exist_ok=True)

    def _ensure_browser(self):
        """Lazy-loads and starts Playwright browser session."""
        if self.page and not self.page.is_closed():
            return True

        try:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()

            # Attempt launch: default chromium -> msedge -> chrome
            launch_channels = [None, "msedge", "chrome"]
            launched = False
            last_err = None

            for ch in launch_channels:
                try:
                    kwargs = {"headless": self.headless, "slow_mo": 100}
                    if ch:
                        kwargs["channel"] = ch
                    self.browser = self.playwright.chromium.launch(**kwargs)
                    launched = True
                    break
                except Exception as ex:
                    last_err = ex
                    continue

            if not launched:
                raise last_err or Exception("Could not launch any browser engine.")

            context = self.browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Task-Worker/1.0"
            )
            self.page = context.new_page()
            self.is_active = True
            return True
        except Exception as e:
            self.is_active = False
            print(f"[BrowserTool] Playwright initialization warning: {e}")
            return False

    def navigate(self, url: str) -> dict:
        """Navigates to the specified URL."""
        if not self._ensure_browser():
            return {"success": False, "error": "Browser engine could not be initialized."}

        try:
            self.page.goto(url, wait_until="networkidle", timeout=10000)
            time.sleep(0.5)
            screenshot = self.capture_screenshot("navigate")
            title = self.page.title()
            return {
                "success": True,
                "url": url,
                "title": title,
                "screenshot": screenshot
            }
        except Exception as e:
            return {"success": False, "error": f"Navigation failed: {str(e)}"}

    def click(self, selector: str, fallback_text: str = None) -> dict:
        """Clicks an element by CSS selector or text."""
        if not self._ensure_browser():
            return {"success": False, "error": "Browser not initialized."}

        try:
            # Try primary selector
            if self.page.is_visible(selector, timeout=3000):
                self.page.click(selector)
            elif fallback_text:
                # Try finding by text
                self.page.click(f"text={fallback_text}", timeout=3000)
            else:
                return {"success": False, "error": f"Element {selector} not found or not visible."}

            try:
                self.page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            time.sleep(0.6)
            screenshot = self.capture_screenshot("click")
            return {"success": True, "selector": selector, "screenshot": screenshot}
        except Exception as e:
            return {"success": False, "error": f"Click failed on {selector}: {str(e)}"}

    def fill(self, selector: str, value: str) -> dict:
        """Fills an input field."""
        if not self._ensure_browser():
            return {"success": False, "error": "Browser not initialized."}

        try:
            self.page.wait_for_selector(selector, state="visible", timeout=4000)
            self.page.fill(selector, str(value))
            return {"success": True, "selector": selector, "value": value}
        except Exception as e:
            return {"success": False, "error": f"Failed to fill {selector}: {str(e)}"}

    def select_option(self, selector: str, value: str) -> dict:
        """Selects an option in a dropdown."""
        if not self._ensure_browser():
            return {"success": False, "error": "Browser not initialized."}

        try:
            self.page.wait_for_selector(selector, state="visible", timeout=4000)
            try:
                self.page.select_option(selector, value=value, timeout=1500)
            except Exception:
                self.page.select_option(selector, label=value, timeout=1500)
            return {"success": True, "selector": selector, "value": value}
        except Exception as e:
            return {"success": False, "error": f"Failed to select {value} in {selector}: {str(e)}"}

    def extract_text(self, selector: str) -> dict:
        """Extracts text content of an element."""
        if not self._ensure_browser():
            return {"success": False, "error": "Browser not initialized."}

        try:
            self.page.wait_for_selector(selector, timeout=4000)
            text = self.page.inner_text(selector)
            return {"success": True, "selector": selector, "text": text.strip()}
        except Exception as e:
            return {"success": False, "error": f"Failed to extract text from {selector}: {str(e)}"}

    def wait_for_any(self, selectors: list, timeout: int = 6000) -> dict:
        """Waits until one of the selectors is visible; returns which one and its text."""
        if not self._ensure_browser():
            return {"success": False, "error": "Browser not initialized."}
        try:
            self.page.wait_for_selector(", ".join(selectors), state="visible", timeout=timeout)
            for sel in selectors:
                if self.page.is_visible(sel):
                    return {"success": True, "selector": sel, "text": self.page.inner_text(sel).strip()}
            return {"success": False, "error": "Selector became hidden before it could be read."}
        except Exception as e:
            return {"success": False, "error": f"None of {selectors} appeared: {str(e)}"}

    def extract_table_rows(self, table_selector: str) -> dict:
        """Extracts table data as a structured list of dicts."""
        if not self._ensure_browser():
            return {"success": False, "error": "Browser not initialized."}

        try:
            self.page.wait_for_selector(table_selector, timeout=4000)
            # Run JS in page to extract clean table headers and cells
            table_data = self.page.evaluate(f"""() => {{
                const table = document.querySelector('{table_selector}');
                if (!table) return [];
                const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.innerText.trim());
                const rows = Array.from(table.querySelectorAll('tbody tr'));
                return rows.map(tr => {{
                    const cells = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim());
                    const obj = {{}};
                    headers.forEach((h, i) => {{ obj[h || ('col_' + i)] = cells[i] || ''; }});
                    return obj;
                }});
            }}""")
            return {"success": True, "rows": table_data}
        except Exception as e:
            return {"success": False, "error": f"Failed to extract table from {table_selector}: {str(e)}"}


    # ------------------------------------------------------------------
    # Generic page understanding: lets the agent DISCOVER a page instead of knowing its selectors
    # ------------------------------------------------------------------
    _SNAPSHOT_JS = """(maxText) => {
      const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const sel = el => {
        if (el.id) return '#' + CSS.escape(el.id);
        const name = el.getAttribute('name');
        if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
        const parts = []; let n = el;
        while (n && n.nodeType === 1 && n !== document.body) {
          let i = 1, s = n; while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;
          parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); n = n.parentElement; }
        return parts.join(' > ');
      };
      const label = el => {
        if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) return l.innerText.trim(); }
        const w = el.closest('label'); if (w) return w.innerText.trim();
        return (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.innerText || el.value || '').trim();
      };
      const elements = [];
      document.querySelectorAll('input,select,textarea,button,a[href]').forEach(el => {
        if (!vis(el) || el.type === 'hidden') return;
        const tag = el.tagName.toLowerCase();
        const item = { selector: sel(el), tag: tag, type: el.type || null, label: label(el).slice(0, 90), disabled: !!el.disabled };
        if (tag === 'a') item.href = el.getAttribute('href');
        else if (tag === 'select') { item.value = el.value; item.options = Array.from(el.options).map(o => ({ value: o.value, label: o.text.trim() })); }
        else if (tag === 'button') item.submits_form = (el.type === 'submit') && !!el.form;
        else item.value = el.value;
        if (el.required) item.required = true;
        if (el.willValidate && !el.validity.valid) item.invalid = el.validationMessage;
        elements.push(item);
      });
      const alerts = Array.from(document.querySelectorAll('[role=alert],.alert')).filter(vis)
        .map(e => e.innerText.trim()).filter(Boolean);
      return { url: location.href, title: document.title, text: document.body.innerText.slice(0, maxText),
               alerts: alerts, elements: elements.slice(0, 80) };
    }"""

    def snapshot(self, max_text: int = 1500) -> dict:
        """Visible text, alert banners and every interactive element (selector, label, value, options)."""
        if not self._ensure_browser():
            return {"success": False, "error": "Browser engine could not be initialized."}
        try:
            snap = self.page.evaluate(self._SNAPSHOT_JS, max_text)
            snap["success"] = True
            snap["screenshot"] = self.capture_screenshot("snapshot")
            return snap
        except Exception as e:
            return {"success": False, "error": f"Could not read the page: {e}"}

    def current_url(self) -> str:
        try:
            return self.page.url if self.page else ""
        except Exception:
            return ""

    def is_submit(self, selector: str) -> bool:
        """True if clicking this element submits a form (i.e. is a potential side effect)."""
        try:
            return bool(self.page.evaluate(
                "(s) => { const el = document.querySelector(s); return !!el && el.type === 'submit' && !!el.form; }",
                selector))
        except Exception:
            return False

    def form_values(self, selector: str) -> dict:
        """Current values of the form that contains `selector`, keyed by field name (or id)."""
        try:
            return self.page.evaluate("""(s) => {
                const el = document.querySelector(s); const f = el && el.closest('form'); if (!f) return {};
                const out = {};
                f.querySelectorAll('input,select,textarea').forEach(i => {
                    const k = i.name || i.id; if (k && i.type !== 'hidden') out[k] = i.value; });
                return out; }""", selector) or {}
        except Exception:
            return {}

    def capture_screenshot(self, tag: str = "shot") -> str:
        """Captures a screenshot of the current page and returns relative web path."""
        if not self.page or self.page.is_closed():
            return None

        filename = f"{tag}_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
        filepath = os.path.join(self.screenshots_dir, filename)
        try:
            self.page.screenshot(path=filepath, full_page=False)
            return f"/static/screenshots/{filename}"
        except Exception as e:
            print(f"[BrowserTool] Screenshot error: {e}")
            return None

    def close(self):
        """Cleans up browser session."""
        try:
            if self.page and not self.page.is_closed():
                self.page.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        finally:
            self.is_active = False
            self.page = None
            self.browser = None
            self.playwright = None
