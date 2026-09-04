// Inspect the generated offline report; never import or run the scheduler.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

async function main() {
  const [reportArgument, outputArgument] = process.argv.slice(2);
  assert(reportArgument && outputArgument, 'Usage: node inspect_visual_report.cjs REPORT OUTPUT');
  const report = path.resolve(reportArgument);
  const output = path.resolve(outputArgument);
  assert(!fs.existsSync(output), 'Choose a new output directory');
  const manifest = JSON.parse(fs.readFileSync(path.join(path.dirname(report), 'manifest.json')));
  const browser = await chromium.launch({
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    headless: true,
  });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, offline: true });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(pathToFileURL(report).href, { waitUntil: 'load' });
    await page.waitForFunction(() => document.getElementById('chain-boundaries')?._fullLayout);
    const observed = await page.evaluate(() => {
      const trend = document.getElementById('trends');
      const boundaries = document.getElementById('chain-boundaries');
      return {
        chart_count: document.querySelectorAll('.js-plotly-plot').length,
        width_node_count: trend.data.filter(t => t.legendgroup === 'width').reduce((n, t) => n + t.x.length, 0),
        boundary_count: boundaries.data.reduce((n, t) => n + t.x.length, 0),
        boundary_sum: boundaries.data.reduce((n, t) => n + t.y.reduce((a, b) => a + b, 0), 0),
        chain_option_count: document.querySelectorAll('#chain-range option').length - 1,
        table_row_count: document.querySelectorAll('#table tbody tr').length,
        has_production_order_note: document.getElementById('sequence').textContent.includes('生产链序'),
        wide_no_overflow: document.documentElement.scrollWidth <= innerWidth,
      };
    });
    assert.equal(observed.chart_count, 4);
    assert.equal(observed.width_node_count, manifest.node_count);
    assert.equal(observed.boundary_count, Math.max(manifest.chain_count - 1, 0));
    assert.equal(observed.boundary_sum, Number(manifest.boundary_total));
    assert.equal(observed.chain_option_count, manifest.chain_count);
    assert.equal(observed.table_row_count, manifest.chain_count);
    assert(observed.has_production_order_note && observed.wide_no_overflow);
    await page.selectOption('#chain-range', '0');
    observed.filtered_range = await page.evaluate(() => document.getElementById('trends').layout.xaxis.range);
    if (manifest.chain_count > 1) assert(observed.filtered_range[1] < manifest.node_count + 0.5);
    await page.selectOption('#chain-range', 'all');
    assert.deepEqual(await page.evaluate(() => document.getElementById('trends').layout.xaxis.range), [0.5, manifest.node_count + 0.5]);
    const point = page.locator('#trends .scatterlayer .trace .points path.point').first();
    await point.scrollIntoViewIfNeeded();
    const box = await point.boundingBox();
    assert(box, 'A real plotted width point must be visible');
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.waitForFunction(() => document.querySelector('#trends .hoverlayer')?.textContent.includes('节点'));
    observed.native_hover = await page.locator('#trends .hoverlayer').textContent();
    await page.mouse.move(0, 0);
    fs.mkdirSync(output, { recursive: true });
    for (const [name, selector] of [['trends', '#sequence'], ['statistics', '#totals'], ['delivery', '#due'], ['boundaries', '#boundaries']]) {
      await page.locator(selector).screenshot({ path: path.join(output, `preview_${name}.png`) });
    }
    await page.setViewportSize({ width: 736, height: 1100 });
    await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
    observed.narrow_no_overflow = true;
    assert.deepEqual(errors, []);
    observed.page_errors = errors;
    observed.report_sha256 = require('node:crypto').createHash('sha256').update(fs.readFileSync(report)).digest('hex');
    fs.writeFileSync(path.join(output, 'browser_check.json'), JSON.stringify(observed, null, 2) + '\n', { flag: 'wx' });
    console.log(JSON.stringify(observed));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
