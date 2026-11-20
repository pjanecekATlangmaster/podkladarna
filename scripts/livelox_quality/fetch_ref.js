// Stáhne mapu třídy z Liveloxu (veřejný prohlížeč) do soukromé cache mimo repozitář:
//   node fetch_ref.js <cache_dir> <classId> [<classId> ...]
// Výstup: <cache_dir>/<classId>/blob.json (projekce mapy) a map.<ext> (obrázek mapy).
// Mapy jsou autorská díla mapařů: jen pro interní měření, nikam je nepublikovat ani necommitovat.
const { chromium } = require('playwright');
const fs = require('fs'); const path = require('path');
(async () => {
  const [cache, ...ids] = process.argv.slice(2);
  const b = await chromium.launch(); const p = await b.newPage();
  for (const id of ids) {
    const dir = path.join(cache, String(id));
    if (fs.existsSync(path.join(dir, 'blob.json')) && fs.readdirSync(dir).some(f => f.startsWith('map.'))) { console.log(id, 'cache'); continue; }
    let info = null;
    const h = async r => { if (/Data\/ClassInfo/.test(r.url())) { try { info = JSON.parse(await r.text()); } catch (e) {} } };
    p.on('response', h);
    await p.goto('https://www.livelox.com/Viewer?classId=' + id, { waitUntil: 'networkidle', timeout: 90000 }).catch(e => console.log(id, e.message));
    await p.waitForTimeout(1500); p.off('response', h);
    if (!info) { console.log(id, 'ERROR bez ClassInfo'); continue; }
    const blob = await p.evaluate(async u => (await fetch(u)).text(), info.general.classBlobUrl);
    let j = null; try { j = JSON.parse(blob); } catch (e) { console.log(id, 'ERROR blob třídy nedostupný'); continue; }
    const m = j.map || (j.maps && j.maps[0]);
    if (!m || !m.url) { console.log(id, 'ERROR bez mapy'); continue; }
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'blob.json'), JSON.stringify({ map: m, event: j.event || null }));
    const buf = await p.evaluate(async u => { const r = await fetch(u); const a = new Uint8Array(await r.arrayBuffer()); let s = ''; for (let i = 0; i < a.length; i += 32768) s += String.fromCharCode.apply(null, a.subarray(i, i + 32768)); return btoa(s); }, m.url);
    const ext = (m.url.split('?')[0].match(/\.(png|jpe?g|gif)$/i) || [, 'png'])[1].toLowerCase();
    fs.writeFileSync(path.join(dir, 'map.' + ext), Buffer.from(buf, 'base64'));
    console.log(id, 'ok', m.projection && m.projection.epsgCode);
  }
  await b.close();
})();
