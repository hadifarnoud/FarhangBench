// Browser half of tests/e2e_review.py: clicks through the reviewer app in Chromium and screenshots every screen at
// 1280 px and 390 px, light and dark. Run by e2e_review.py with E2E_CONFIG (JSON) in the environment; Playwright is
// loaded from cfg.playwrightDir so the repo doesn't depend on it. Writes browser-report.json to cfg.outDir.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const cfg = JSON.parse(process.env.E2E_CONFIG);
const require = createRequire(path.join(cfg.playwrightDir, "package.json"));
const { chromium } = require("playwright");

const steps = [], screenshots = [], consoleLog = [], problems = [];
let expectHttpErrors = 0;
const SIZES = [["desktop", 1280, 800], ["mobile", 390, 844]];
const SCHEMES = ["light", "dark"];

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, colorScheme: "light", deviceScaleFactor: 1 });
await context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
await context.addInitScript(() => {
  document.addEventListener("securitypolicyviolation", e => console.error(`CSP violation: ${e.violatedDirective} blocked ${e.blockedURI || "inline"}`));
});
const page = await context.newPage();
page.on("console", msg => {
  const text = msg.text(), type = msg.type();
  consoleLog.push({ type, text, url: page.url() });
  const http = /Failed to load resource: the server responded with a status of (401|403|429)/.test(text);
  if (type === "error" && !(http && expectHttpErrors > 0)) problems.push({ type, text, url: page.url() });
  if (type === "warning" && /Content.Security.Policy/i.test(text)) problems.push({ type, text, url: page.url() });
});
page.on("pageerror", err => problems.push({ type: "pageerror", text: String(err.stack || err), url: page.url() }));

async function step(name, fn) {
  const t0 = Date.now();
  try {
    const detail = await fn();
    steps.push({ name, ok: true, ms: Date.now() - t0, detail: detail ?? null });
    console.log(`  ok    ${name}`);
  } catch (err) {
    const file = `FAILED-${steps.length + 1}.png`;
    await page.screenshot({ path: path.join(cfg.outDir, file) }).catch(() => {});
    steps.push({ name, ok: false, ms: Date.now() - t0, error: String(err.stack || err), screenshot: file });
    console.log(`  FAIL  ${name}: ${err.message}`);
    throw err;
  }
}
function expect(cond, message) { if (!cond) throw new Error(message); }

// Screenshot the current screen in all four variants. `again` re-opens transient UI (menus close on resize).
async function snap(name, again) {
  for (const [tag, width, height] of SIZES) {
    for (const scheme of SCHEMES) {
      await page.setViewportSize({ width, height });
      await page.emulateMedia({ colorScheme: scheme });
      if (again) await again();
      await page.waitForTimeout(150);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      if (overflow > 1) {
        const culprits = await page.evaluate(() => [...document.querySelectorAll("body *")]
          .filter(el => el.getBoundingClientRect().right > window.innerWidth + 1)
          .slice(-5).map(el => `${el.tagName.toLowerCase()}.${[...el.classList].join(".")} right=${Math.round(el.getBoundingClientRect().right)}`));
        throw new Error(`${name} at ${width}px scrolls sideways by ${overflow}px: ${culprits.join("; ")}`);
      }
      const file = `${name}--${tag}-${scheme}.png`;
      await page.screenshot({ path: path.join(cfg.outDir, file) });
      screenshots.push(file);
    }
  }
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.emulateMedia({ colorScheme: "light" });
  if (again) await again();
}
const go = async hash => { await page.evaluate(h => { location.hash = h; }, hash); };
const settle = () => page.waitForLoadState("networkidle").catch(() => {});

let failed = false;
try {
  await step("sign-in page loads without a Basic-auth popup", async () => {
    const res = await page.goto(cfg.base + "/");
    expect(res.status() === 200, "GET / status");
    await page.waitForSelector("#login-form");
    expect(await page.locator("#li-name").evaluate(el => el === document.activeElement), "name field focused");
    await snap("01-login");
  });

  await step("wrong password shows an error", async () => {
    expectHttpErrors++;
    await page.fill("#li-name", cfg.admin);
    await page.fill("#li-pass", "definitely-wrong");
    await page.press("#li-pass", "Enter");
    await page.waitForSelector("#li-error:not([hidden])");
    expect((await page.textContent("#li-error")).includes("Wrong name or password"), "error text");
    await page.click("[data-act=pwtoggle]");
    expect(await page.getAttribute("#li-pass", "type") === "text", "show/hide password");
    await snap("02-login-error");
    expectHttpErrors--;
  });

  await step("admin signs in", async () => {
    await page.fill("#li-pass", cfg.adminPassword);
    await page.click("#li-btn");
    await page.waitForSelector("#queue-list .qrow", { timeout: 30000 });
    await page.waitForSelector("#start-btn");
    expect(await page.isVisible("#admin-menu-btn"), "admin menu visible");
    await snap("03-review-start");
  });

  await step("admin menu", async () => {
    const open = async () => {
      if (!(await page.isVisible(".menu:not([hidden]) a[href='#/admin/people']"))) await page.click("#admin-menu-btn");
      await page.waitForSelector(".menu:not([hidden]) a[href='#/admin/people']");
    };
    await open();
    await snap("04-admin-menu", open);
    await page.keyboard.press("Escape");
    await page.click("body", { position: { x: 5, y: 400 } });
  });

  await step("People: create a user, one-time password shown once", async () => {
    await go("#/admin/people");
    await page.waitForSelector("#add-person");
    await settle();
    await snap("10-people");
    await page.click("#add-person");
    await page.waitForSelector("#add-form");
    await page.fill("#add-form input[name=name]", cfg.newUser);
    await page.fill("#add-form input[name=display_name]", cfg.newUserDisplay);
    await snap("11-people-add-dialog");
    await page.click("#add-submit");
    await page.waitForSelector("#otp");
    cfg.otp = (await page.textContent("#otp")).trim();
    expect(cfg.otp.length >= 10, "one-time password");
    const msg = await page.inputValue("#signin-msg");
    expect(msg.includes(cfg.otp) && msg.includes(cfg.base), "sign-in message has the password and the page URL");
    await page.click("[data-copy=otp]");
    await page.waitForSelector(".toast");
    await snap("12-people-one-time-password");
    await page.click("dialog button[value=done]");
    await page.waitForSelector(`tr[data-user="${cfg.newUser}"]`);
    expect((await page.textContent(`tr[data-user="${cfg.newUser}"]`)).includes("awaiting first sign-in"), "new user status");
    const open = async () => {
      if (await page.isVisible(".menu:not([hidden]) [data-op=reset]")) return;
      await page.click(`tr[data-user="${cfg.seededReviewer}"] [data-act=menu]`);
      await page.waitForSelector(".menu:not([hidden]) [data-op=reset]");
    };
    await open();
    await snap("13-people-row-menu", open);
    await page.keyboard.press("Escape");
    await page.click("h1");
    return `one-time password for ${cfg.newUser}`;
  });

  await step("Activity feed", async () => {
    await go("#/admin/activity");
    await page.waitForSelector(".feed li");
    expect((await page.textContent(".feed")).includes(cfg.seededReviewer), "seeded reviewer's work shows");
    await snap("14-activity");
  });

  await step("Reviewers table and reviewer page", async () => {
    await go("#/admin/reviewers");
    await page.waitForSelector("table.table tbody tr");
    await snap("15-reviewers");
    await page.click(`a[href="#/admin/reviewers/${cfg.seededReviewer}"]`);
    await page.waitForSelector(".profile-head");
    await page.waitForSelector(".feed li");
    await snap("16-reviewer-page");
  });

  await step("Items explorer", async () => {
    await go("#/admin/items");
    await page.waitForSelector("tr[data-act=audit-open]", { timeout: 30000 });
    await snap("17-items");
  });

  await step("Item audit asks before showing the key, then shows every review", async () => {
    await go(`#/admin/items?q=${encodeURIComponent(cfg.reviewedItem)}`);
    await page.waitForSelector(`tr[data-id="${cfg.reviewedItem}"]`);
    await page.click(`tr[data-id="${cfg.reviewedItem}"]`);
    await page.waitForSelector("dialog[open] [data-role=ok]");
    expect((await page.textContent("dialog[open]")).includes("won't be able to review this item blind"), "peek warning text");
    await snap("18-item-audit-confirm");
    await page.click("dialog[open] [data-role=ok]");
    await page.waitForSelector(".review-cards .rcard");
    const text = await page.textContent(".review-cards");
    expect(text.includes(cfg.seededReviewer) && text.includes("Wrong"), "seeded review listed with its verdict");
    expect(await page.isVisible(".option.is-key"), "key marked");
    await snap("19-item-audit");
    await go(`#/review/${encodeURIComponent(cfg.reviewedItem)}`);
    await page.waitForSelector(".notice.t-violet");
    return "peek recorded; review page now refuses a blind answer";
  });

  await step("Audit log shows the peek", async () => {
    await go("#/admin/audit");
    await page.waitForSelector("table.table tbody tr");
    expect((await page.textContent("table.table")).includes("Viewed a key"), "peek row");
    await snap("20-audit-log");
  });

  await step("Progress", async () => {
    await go("#/progress");
    await page.waitForSelector(".tiles");
    await snap("21-progress");
  });

  await step("user menu and sign out", async () => {
    await go("#/review");
    await page.waitForSelector("#queue-list .qrow");
    const open = async () => { if (!(await page.isVisible("#signout-btn"))) await page.click("#user-menu-btn"); await page.waitForSelector("#signout-btn:visible"); };
    await open();
    await snap("22-user-menu", open);
    await page.click("#signout-btn");
    await page.waitForSelector("#login-form");
    const who = await page.evaluate(() => fetch("/api/whoami").then(r => r.json()));
    expect(!who.signed_in, "signed out");
  });

  await step("new reviewer signs in and must choose a password", async () => {
    await page.fill("#li-name", cfg.newUser);
    await page.fill("#li-pass", cfg.otp);
    await page.click("#li-btn");
    await page.waitForSelector("#pw-form");
    await page.fill("#pw1", "short");
    await snap("05-choose-password");
    await page.fill("#pw1", cfg.newUserPassword);
    await page.fill("#pw2", cfg.newUserPassword);
    await page.click("#pw-save");
    await page.waitForSelector("#queue-list .qrow", { timeout: 30000 });
    expect(!(await page.isVisible("#admin-menu-btn")), "reviewer has no admin menu");
  });

  let firstUrl = "";
  await step("review an item blind with the keyboard", async () => {
    await go(`#/review/${encodeURIComponent(cfg.browserItem)}`);
    await page.waitForSelector(".option[data-act=pick]");
    expect(!(await page.isVisible(".verdict-card")), "no key before answering");
    await page.click("h1");
    await page.keyboard.press("2");
    expect(await page.getAttribute(".option[data-i='1']", "aria-checked") === "true", "2 picks option 2");
    await snap("06-review-blind");
    firstUrl = page.url();
    await page.keyboard.press("Enter");
    await page.waitForSelector(".verdict-card");
    expect(await page.isVisible(".option.is-key"), "key shown after answering");
  });

  await step("judge the key: verdict, flag, comment, auto-advance", async () => {
    await page.keyboard.press("w");
    expect(await page.getAttribute(".vbtn[data-v=wrong]", "aria-pressed") === "true", "w = wrong");
    await page.keyboard.press("c");
    expect(await page.getAttribute(".vbtn[data-v=correct]", "aria-pressed") === "true", "c = correct");
    await page.click(".fchip[data-f=unnatural_persian]");
    await page.fill("#comment", "منبع: لغت‌نامهٔ دهخدا");
    await page.click("h1");
    await snap("07-review-key");
    await page.click("#save-verdict");
    await page.waitForSelector(".toast");
    await page.waitForFunction(u => location.href !== u, firstUrl);
    await page.waitForSelector(".option[data-act=pick], #ans");
    expect(!page.url().endsWith(encodeURIComponent(cfg.browserItem)), "moved to the next item");
    return `advanced to ${decodeURIComponent(page.url().split("#/review/")[1] || "")}`;
  });

  await step("keyboard shortcut help", async () => {
    await page.click("h1");
    await page.keyboard.press("?");
    await page.waitForSelector("dialog[open] .help-grid");
    await snap("08-shortcuts", async () => { if (!(await page.isVisible("dialog[open]"))) { await page.click("h1"); await page.keyboard.press("?"); await page.waitForSelector("dialog[open]"); } });
    await page.keyboard.press("Escape");
  });

  await step("My work lists the review", async () => {
    await go("#/me");
    await page.waitForSelector(".tiles");
    expect((await page.textContent(".feed")).includes(cfg.browserItem), "history has the item");
    await snap("09-my-work");
    await page.click(`.feed a[href="#/review/${encodeURIComponent(cfg.browserItem)}"]`);
    await page.waitForSelector(".verdict-card");
    expect(await page.getAttribute(".vbtn[data-v=correct]", "aria-pressed") === "true", "revisit shows the saved verdict");
  });

  await step("change password page", async () => {
    await go("#/password");
    await page.waitForSelector("#pw0");
    await snap("23-change-password");
  });

  await step("reviewer can't open admin pages", async () => {
    await go("#/admin/people");
    await page.waitForSelector("text=Admins only");
  });

  await step("sign out", async () => {
    await go("#/review");
    await page.click("#user-menu-btn");
    await page.click("#signout-btn");
    await page.waitForSelector("#login-form");
  });

  await step("local mode: no sign-in, name in the top bar", async () => {
    await page.goto(cfg.localBase + "/");
    await page.waitForSelector("#who");
    await snap("30-local-welcome");
    await page.fill("#who", "local tester");
    await page.press("#who", "Enter");
    await page.waitForSelector("#queue-list .qrow", { timeout: 30000 });
    expect(await page.inputValue("#who") === "local-tester", "name normalised");
    expect(await page.isVisible("#admin-menu-btn"), "admin views available locally");
    await page.click("#start-btn");
    await page.waitForSelector(".option[data-act=pick], #ans");
    await snap("31-local-review");
  });
} catch (err) {
  failed = true;
}

steps.push({ name: "no console errors or CSP violations", ok: problems.length === 0, ms: 0,
  error: problems.length ? JSON.stringify(problems.slice(0, 20), null, 1) : undefined });
await browser.close();
fs.writeFileSync(path.join(cfg.outDir, "browser-report.json"), JSON.stringify({ steps, screenshots, console: consoleLog, problems }, null, 2));
process.exit(failed || problems.length ? 1 : 0);
