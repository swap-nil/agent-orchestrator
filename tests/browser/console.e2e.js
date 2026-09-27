// Browser test of the command center (requires Node and Playwright: npm i -D playwright && npx playwright install chromium).
// Live:  make console, then: node tests/browser/console.e2e.js http://127.0.0.1:8765 live /tmp
// Demo:  serve the page statically and run with tag "demo" (see docs/COMMAND_CENTER.md).
const { chromium } = require('playwright');
const base = process.argv[2], tag = process.argv[3], SP = process.argv[4];
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/fonts\.g/.test(m.text()) && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await page.route(/fonts\.(googleapis|gstatic)\.com/, r => r.abort());
  await page.goto(base + (tag === 'demo' ? '/console/' : '/console'));
  await page.waitForSelector('#chOutcomes svg', { timeout: 15000 });
  await page.waitForTimeout(tag === 'live' ? 9000 : 1500);
  await page.evaluate(() => refresh(true)); await page.waitForTimeout(800);
  console.log(tag, 'mode:', await page.textContent('#modeText'));
  console.log(tag, 'tiles:', (await page.$$eval('.tile .value', els => els.map(e => e.textContent.trim()))).join(' | '));
  await page.screenshot({ path: `${SP}/${tag}-overview.png` });
  // live decisions + inspector
  await page.click('[data-view=live]'); await page.waitForTimeout(1200);
  const rows = await page.$$('#feedBody tr[data-tid]:not([data-tid=""])');
  console.log(tag, 'feed rows:', rows.length);
  const turnRow = await page.$('#feedBody tr[data-tid]:not([data-tid=""])');
  await turnRow.click(); await page.waitForSelector('.drawer .timeline', { timeout: 5000 });
  console.log(tag, 'inspector stages:', (await page.$$eval('.stage h4', e => e.map(x => x.childNodes[0].textContent.trim()))).join(' > '));
  console.log(tag, 'chain:', await page.textContent('.drawer .kv dd:last-child'));
  await page.screenshot({ path: `${SP}/${tag}-inspector.png` });
  await page.click('#dClose');
  for (const v of ['agents', 'safeguards', 'evals', 'studio', 'controls', 'audit']) {
    await page.click(`[data-view=${v}]`); await page.waitForTimeout(1300);
    await page.screenshot({ path: `${SP}/${tag}-${v}.png`, fullPage: v !== 'audit' });
  }
  // kill switch: disable market-agent in controls
  await page.click('[data-view=controls]'); await page.waitForTimeout(1200);
  await page.click('input[data-grp=disabled_agents][data-name=market-agent]'); await page.waitForTimeout(600);
  await page.click('#ksApply'); await page.fill('#mReason', 'x'); await page.click('#mOk');
  console.log(tag, 'short reason:', await page.textContent('#mErr'));
  await page.fill('#mReason', 'INC-4471 market data vendor outage'); await page.click('#mOk'); await page.waitForTimeout(1200);
  const ks = await page.evaluate(() => STATE.overview.kill_switch.disabled_agents);
  console.log(tag, 'kill switch now:', JSON.stringify(ks));
  // skill studio: fix net worth routing
  await page.click('[data-view=studio]'); await page.waitForTimeout(1200);
  await page.click('[data-sel="portfolio.overview"]'); await page.waitForTimeout(400);
  const ta = await page.$('textarea[data-k="intent|portfolio.overview|patterns"]');
  await ta.click(); await page.keyboard.press('Control+End'); await page.keyboard.type('\n\\bmy net worth\\b');
  await page.click('#tryBtn'); await page.waitForTimeout(700);
  console.log(tag, 'try:', (await page.textContent('#tryOut')).replace(/\s+/g, ' ').trim());
  await page.fill('#chTitle', 'Recognise "my net worth"'); await page.fill('#chReason', 'Customers say "my net worth"; they reach the FAQ today');
  await page.click('#proposeBtn'); await page.waitForSelector('#changeCard .gate', { timeout: 8000 });
  console.log(tag, 'gate:', (await page.textContent('#changeCard .gate')).replace(/\s+/g, ' ').trim());
  console.log(tag, 'approve disabled for proposer:', await page.$eval('#cApprove', b => b.disabled + ' / ' + b.title));
  await page.click('#cShadow'); await page.waitForTimeout(tag === 'live' ? 5000 : 3500);
  // switch operator to Anna (approver)
  await page.selectOption('#operatorSel', 'anna'); await page.waitForTimeout(1200);
  await page.click('[data-chg]'); await page.waitForTimeout(800);
  console.log(tag, 'shadow:', ((await page.$('#changeCard .callout:last-of-type')) ? (await page.textContent('#changeCard')).match(/Shadow on live traffic[^.]*/)?.[0] : 'n/a'));
  await page.click('#cApprove'); await page.click('#mOk'); await page.waitForTimeout(1500);
  console.log(tag, 'versions:', (await page.$$eval('.ver', e => e.map(x => x.textContent.replace(/\s+/g, ' ').trim().slice(0, 60)))).join(' | '));
  // Anna cannot use the kill switch (no operator role)
  await page.click('[data-view=controls]'); await page.waitForTimeout(1000);
  console.log(tag, 'anna kill toggle disabled:', await page.$eval('input[data-grp=disabled_agents]', i => i.disabled));
  // Olga rolls back
  await page.selectOption('#operatorSel', 'olga'); await page.waitForTimeout(1000);
  await page.click('[data-view=studio]'); await page.waitForTimeout(1200);
  await page.click('[data-rollback="0"]'); await page.fill('#mReason', 'Rehearsing rollback during the demo'); await page.click('#mOk'); await page.waitForTimeout(1500);
  console.log(tag, 'after rollback active:', await page.evaluate(() => VERSIONS.active));
  await page.click('[data-view=evals]'); await page.waitForTimeout(1500);
  console.log(tag, 'evals latest:', (await page.textContent('#evalsHost .value')).trim(), '| runs:', (await page.$$('#evalsHost section:last-child tbody tr')).length);
  await page.click('[data-view=audit]'); await page.waitForTimeout(1500);
  console.log(tag, 'audit:', (await page.$$eval('#auditHost tbody tr td:nth-child(3)', e => e.slice(0, 12).map(x => x.textContent))).join(', '));
  console.log(tag, 'audit chain:', (await page.textContent('#auditHost .panel-h .right')).replace(/\s+/g, ' ').trim());
  // phone width + dark
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.setViewportSize({ width: 400, height: 860 });
  await page.click('[data-view=overview]'); await page.waitForTimeout(1500);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  console.log(tag, 'phone horizontal overflow px:', overflow);
  await page.screenshot({ path: `${SP}/${tag}-phone-dark.png`, fullPage: false });
  console.log(tag, 'ERRORS:', errors.length ? errors.slice(0, 8) : 'none');
  await browser.close();
})().catch(e => { console.error('FAILED', e); process.exit(1); });
