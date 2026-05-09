"""Build a static snapshot of camwatch for public demo at camwatch-demo.leidevs.com.

Renders the camwatch web UI as a fully static site under dist/, scoped to today
and yesterday's captures. Designed to deploy to Cloudflare Pages.

Run:
    python scripts/build_demo.py [--source ../camwatch] [--out dist] [--today YYYY-MM-DD]

Defaults assume this repo lives next to the camwatch source checkout.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape


# --- CLI / paths ----------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="../camwatch", help="path to camwatch checkout")
    p.add_argument("--out", default="dist", help="output dir for static site")
    p.add_argument("--today", default=None, help="YYYY-MM-DD; default = today (local)")
    p.add_argument("--days", type=int, default=2, help="number of days to include (default 2 = today+yesterday)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    if not source.exists():
        sys.exit(f"source not found: {source}")

    sys.path.insert(0, str(source))
    today = date.fromisoformat(args.today) if args.today else datetime.now().astimezone().date()
    print(f"build: source={source}  out={out}  today={today.isoformat()}  span_days={args.days}")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # Import after sys.path is set; monkey-patch _today_local before importing
    # the server module so its module-level constants don't get computed against
    # the wall clock.
    from camwatch import server as cw_server  # type: ignore
    from camwatch.config import load_config  # type: ignore
    from camwatch.db import Database  # type: ignore

    cw_server._today_local = lambda: today  # date-lock for heatmap/window math
    cw_server.PAGE_SIZE = 10000             # one page contains all 2-day rows

    cfg = load_config(source / "config" / "config.yaml")
    db = Database(source / "camwatch.db")

    span_start_dt = datetime.combine(today - timedelta(days=args.days - 1), datetime.min.time())
    span_end_dt = datetime.combine(today + timedelta(days=1), datetime.min.time())
    span_start_iso = span_start_dt.isoformat(timespec="seconds")
    span_end_iso = span_end_dt.isoformat(timespec="seconds")
    print(f"window: [{span_start_iso}, {span_end_iso})")

    # Pull passes for the span. We render with alerts_only=False so the demo
    # surfaces every pass; the visitor can see what filters are visually
    # available even though they don't actively filter the static list.
    cal = cfg.load_calibration()
    dist_n = cal.line_distance_m_north if cal else 0
    dist_s = cal.line_distance_m_south if cal else 0
    threshold = cfg.alert_threshold_mph

    span_rows = db.list_passes(
        alerts_only=False,
        threshold_mph=threshold,
        line_distance_m_north=dist_n,
        line_distance_m_south=dist_s,
        limit=100000,
        include_deleted=False,
        time_ranges=[(span_start_iso, span_end_iso)],
    )
    print(f"passes in window: {len(span_rows)}")

    rendered = [cw_server.render_pass(p, dist_n, dist_s, threshold) for p in span_rows]

    # Heatmap: trailing 7 days, but constrained to the same window so cells
    # outside the demo span show empty (consistent with the data we ship).
    heatmap_ctx = cw_server._build_heatmap(rendered, today, selected=None)

    # Histogram: all rendered (no bucket selected = all-default state).
    histogram, hist_total, hist_all_default = cw_server._build_histogram(
        rendered, threshold, selected_buckets=set()
    )

    # --- render templates -------------------------------------------------
    templates_dir = source / "camwatch" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )

    static_v = str(int((source / "camwatch" / "static" / "style.css").stat().st_mtime))

    # Pull homography meta the same way the server does, so the header pill
    # ("13 anchors / N cm mean err") still renders.
    homog_path = source / "config" / "homography.yaml"
    homog_meta: dict = {}
    if homog_path.exists():
        try:
            with homog_path.open() as f:
                hd = (yaml.safe_load(f) or {}).get("homography", {}) or {}
            homog_meta = {
                "homog_n_pts": len(hd.get("pixel_pts_sub") or []),
                "homog_mean_err_cm": float(hd.get("mean_reprojection_error_m") or 0.0) * 100.0,
            }
        except Exception:
            homog_meta = {}

    common_ctx = {
        "static_v": static_v,
        "rows": rendered,
        "threshold": threshold,
        "retention_days": cfg.retention_days,
        "clip_margin_s": cfg.clip_margin_s,
        "clip_capture_min_mph": cfg.clip_capture_min_mph,
        "clip_capture_max_mph": cfg.clip_capture_max_mph,
        "preview_show_grid": cfg.preview_show_grid,
        "pause_at_night": cfg.pause_at_night,
        "running": True,
        "paused_night": False,
        "histogram": histogram,
        "histogram_total": hist_total,
        "histogram_all_default": hist_all_default,
        "page": 1,
        "total_pages": 1,
        "total_filtered": len(rendered),
        "include_oob_histogram": True,
        "include_oob_heatmap": True,
        **homog_meta,
        **heatmap_ctx,
    }

    # Index inlines _pass_list.html via Jinja {% include %}. Disable the
    # OOB histogram/heatmap there to avoid duplicate IDs (the sidebar already
    # renders them once); the standalone passes.html — the response HTMX would
    # have swapped in — keeps them on for completeness even though our client
    # filter intercepts the request.
    index_ctx = {**common_ctx, "include_oob_histogram": False, "include_oob_heatmap": False}
    index_html = env.get_template("index.html").render(**index_ctx)
    pass_list_html = env.get_template("_pass_list.html").render(**common_ctx)

    # Per-pass metadata for the client-side filter engine. Keys are short to
    # keep the inline JSON small (~30 KB for 700 rows).
    week_start, _ = cw_server._week_window(today)
    passes_meta = {}
    for r in rendered:
        mph = r.get("computed_mph")
        passes_meta[str(r["id"])] = {
            "d": r["direction"],
            "m": float(mph) if mph is not None and mph > 0 else None,
            "a": 1 if r.get("alert") else 0,
            "b": cw_server._bucket_for(mph) if mph is not None and mph > 0 else None,
            "s": cw_server._slot_index_for(r["captured_at"], week_start),
        }

    index_html = patch_index_html(
        index_html,
        latest_thumb_url="/preview/stream",
        today=today,
        n_passes=len(rendered),
        passes_meta=passes_meta,
    )

    (out / "index.html").write_text(index_html)
    # Cloudflare Pages resolves /passes -> passes.html, while /passes/{id}/clip
    # resolves into the directory below — so file + directory can coexist.
    (out / "passes.html").write_text(pass_list_html)
    print(f"wrote: index.html ({len(index_html):,} bytes), passes.html ({len(pass_list_html):,} bytes)")

    # --- copy static assets ----------------------------------------------
    static_src = source / "camwatch" / "static"
    static_dst = out / "static"
    static_dst.mkdir(parents=True, exist_ok=True)
    for f in static_src.iterdir():
        shutil.copy2(f, static_dst / f.name)
    print(f"copied static/ ({sum(1 for _ in static_dst.iterdir())} files)")

    # --- per-pass binary assets ------------------------------------------
    recordings_dir = source / "recordings"
    events_dir = source / "events"
    n_clip = n_thumb = n_big = n_traj = 0
    latest_thumb: Path | None = None
    latest_clip: Path | None = None
    for p in span_rows:
        pdir = out / "passes" / str(p.id)
        pdir.mkdir(parents=True, exist_ok=True)
        if p.clip_path:
            clip_src = Path(p.clip_path)
            if not clip_src.is_absolute():
                clip_src = source / clip_src
            base = clip_src.with_suffix("")  # strip .mp4
            if clip_src.exists():
                shutil.copy2(clip_src, pdir / "clip")
                n_clip += 1
                if latest_clip is None:
                    latest_clip = pdir / "clip"
            thumb_src = base.with_suffix(".jpg")
            if thumb_src.exists():
                shutil.copy2(thumb_src, pdir / "thumb")
                n_thumb += 1
                if latest_thumb is None:
                    latest_thumb = pdir / "thumb"
            big_src = base.parent / (base.name + "_big.jpg")
            if big_src.exists():
                shutil.copy2(big_src, pdir / "thumb_big")
                n_big += 1
            elif thumb_src.exists():
                # Match camwatch's server.py behavior: when no _big variant is
                # on disk, fall through to the regular thumb so the expanded
                # image-wrap has something to render (browser upscales).
                shutil.copy2(thumb_src, pdir / "thumb_big")
        traj_src = events_dir / f"pass_{p.id}.jsonl"
        if traj_src.exists():
            shutil.copy2(traj_src, pdir / "trajectory.jsonl")
            n_traj += 1
    print(f"per-pass assets: {n_clip} clips, {n_thumb} thumbs, {n_big} big-thumbs, {n_traj} trajectories")

    # --- singleton API stubs ---------------------------------------------
    api_dir = out / "api"
    api_dir.mkdir(parents=True, exist_ok=True)

    if homog_path.exists():
        hd_full = (yaml.safe_load(homog_path.read_text()) or {}).get("homography", {}) or {}
        api_homog = {
            "H": hd_full["H"],
            "frame_size_sub": hd_full.get("frame_size_sub", [640, 480]),
            "main_to_sub_scale": hd_full.get("main_to_sub_scale", 3.2),
            "spacing_ft": hd_full.get("spacing_ft", 5.0),
            "road_width_ft": hd_full.get("road_width_ft", 30.0),
            "pixel_pts_sub": hd_full.get("pixel_pts_sub", []),
            "meter_pts": hd_full.get("meter_pts", []),
        }
        (api_dir / "homography").write_text(json.dumps(api_homog))
        print("wrote: api/homography")

    api_status = {
        "running": False,
        "threshold_mph": threshold,
        "line_distance_m_north": dist_n,
        "line_distance_m_south": dist_s,
        "known_count": len(cal.calibration_points) if cal else 0,
    }
    (api_dir / "status").write_text(json.dumps(api_status))

    status_badge_html = env.get_template("_status_badge.html").render(running=False, paused_night=False)
    (out / "status-badge").write_text(status_badge_html)

    # --- preview/stream as a still JPG, with "Static Preview" burned in ---
    preview_dir = out / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    if latest_thumb is not None:
        write_preview_with_overlay(latest_thumb, preview_dir / "stream")
        print(f"wrote: preview/stream from {latest_thumb.name} (with overlay)")
    else:
        print("warning: no thumb found for preview/stream")

    # --- Cloudflare Pages headers ----------------------------------------
    headers = """\
/passes/*/clip
  Content-Type: video/mp4
  Cache-Control: public, max-age=31536000, immutable
/passes/*/thumb
  Content-Type: image/jpeg
  Cache-Control: public, max-age=31536000, immutable
/passes/*/thumb_big
  Content-Type: image/jpeg
  Cache-Control: public, max-age=31536000, immutable
/passes/*/trajectory.jsonl
  Content-Type: application/x-ndjson
  Cache-Control: public, max-age=31536000, immutable
/api/homography
  Content-Type: application/json
/api/status
  Content-Type: application/json
/status-badge
  Content-Type: text/html; charset=utf-8
/preview/stream
  Content-Type: image/jpeg
  Cache-Control: public, max-age=300
"""
    (out / "_headers").write_text(headers)
    print("wrote: _headers")

    # --- summary ----------------------------------------------------------
    total_files = sum(1 for _ in out.rglob("*") if _.is_file())
    total_bytes = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"\ndone: {total_files} files, {total_bytes / 1e6:.1f} MB in {out}")


# --- preview overlay ------------------------------------------------------

def write_preview_with_overlay(src: Path, dst: Path) -> None:
    """Burn a 'STATIC PREVIEW' label into the JPG so the side note isn't needed.

    Uses cv2 (already a camwatch dep) to draw a translucent dark band across
    the top with white sans-serif text.
    """
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        # Fallback: just copy the file unchanged.
        shutil.copy2(src, dst)
        return
    h, w = img.shape[:2]
    text = "STATIC PREVIEW"
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.4, w / 640.0 * 0.7)
    thickness = max(1, int(round(scale * 2)))
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    pad_x, pad_y = int(scale * 12), int(scale * 8)
    band_h = th + baseline + pad_y * 2

    # Translucent dark band across the top.
    overlay = img.copy()
    cv2.rectangle(overlay, (0, 0), (w, band_h), (0, 0, 0), thickness=cv2.FILLED)
    cv2.addWeighted(overlay, 0.55, img, 0.45, 0, dst=img)

    # White text, centered horizontally.
    tx = (w - tw) // 2
    ty = pad_y + th
    cv2.putText(img, text, (tx, ty), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)

    # Encode to JPEG bytes manually: cv2.imwrite picks the codec by extension,
    # but our destination is extensionless (Cloudflare Pages routes /preview/stream
    # to this file with content-type set via _headers).
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        shutil.copy2(src, dst)
        return
    dst.write_bytes(buf.tobytes())


# --- index.html patches ---------------------------------------------------

def patch_index_html(
    html: str,
    *,
    latest_thumb_url: str,
    today: date,
    n_passes: int,
    passes_meta: dict,
) -> str:
    """Patch the rendered index.html for the static demo:
      1. Replace ?big=1 query string with the _big path suffix (CF Pages
         ignores query strings during file lookup).
      2. Inject a top banner.
      3. Replace the live preview <img> with a still + note.
      4. Inject a toast shim that intercepts htmx POSTs.
      5. Inject window.PASSES metadata + a client-side filter engine that
         takes over the existing htmx GET requests.
      6. Strip the form's "every 10s" auto-refresh (no live data to fetch).
    """
    # 1. ?big=1 → _big
    html = html.replace("/thumb?big=1", "/thumb_big")

    # 2. Banner — inject right after <body>
    banner = f"""\
<div id="demo-banner" style="background:#1a4ed8;color:#fff;padding:8px 14px;font:14px/1.4 system-ui,sans-serif;text-align:center;">
  <strong>camwatch · public read-only snapshot</strong> ·
  showing {n_passes} captures from {(today - timedelta(days=1)).isoformat()} to {today.isoformat()} ·
  filters work locally, write actions are no-ops · live source at
  <a href="https://camwatch.leidevs.com/" style="color:#fff;text-decoration:underline;">camwatch.leidevs.com</a>
  · code at
  <a href="https://github.com/leochen4891/camwatch" style="color:#fff;text-decoration:underline;">github.com/leochen4891/camwatch</a>
</div>
"""
    html = html.replace("<body>", "<body>\n" + banner, 1)

    # 6. Strip auto-refresh — leave "change" so client filter still triggers.
    html = html.replace(
        'hx-trigger="change, every 10s"',
        'hx-trigger="change"',
    )

    # 3. Live preview pane — swap the empty <img> for the static still (the
    # "Static Preview" label is burned into the JPG itself, so no side text).
    # Also neutralize the runtime's src-removal logic (the upstream UI strips
    # the src when hidden to halt the MJPEG stream — we want the still to
    # persist) and default the preview to "visible" so the snapshot shows on
    # first paint without the user clicking "show".
    preview_old = '<img alt="live preview" id="preview-img">'
    preview_new = (
        f'<img alt="static snapshot of last captured frame" '
        f'id="preview-img" src="{latest_thumb_url}">'
    )
    html = html.replace(preview_old, preview_new)
    html = html.replace(
        'img.removeAttribute("src");',
        '/* demo: keep static still */',
    )
    html = html.replace(
        'setPreviewVisible(stored === "1");',
        'setPreviewVisible(true);  /* demo: always show static still */',
    )

    # 4. JS shim — intercept POSTs, show a toast, never hit the network.
    shim = """\
<script>
(function () {
  const toastBox = document.createElement("div");
  toastBox.id = "demo-toast";
  toastBox.style.cssText =
    "position:fixed;bottom:24px;left:50%;transform:translateX(-50%);" +
    "background:#1a4ed8;color:#fff;padding:10px 16px;border-radius:6px;" +
    "font:14px system-ui;box-shadow:0 4px 12px rgba(0,0,0,.2);" +
    "opacity:0;transition:opacity .2s;pointer-events:none;z-index:9999;";
  document.body.appendChild(toastBox);
  function toast(msg) {
    toastBox.textContent = msg;
    toastBox.style.opacity = "1";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (toastBox.style.opacity = "0"), 2200);
  }
  document.body.addEventListener("htmx:beforeRequest", function (evt) {
    const verb = (evt.detail.requestConfig && evt.detail.requestConfig.verb) || "";
    if (verb.toLowerCase() === "post") {
      evt.preventDefault();
      toast("Read-only demo — write actions are disabled.");
    }
  });
  // Also catch native form submits (settings dialog uses <form hx-post>).
  document.addEventListener("submit", function (evt) {
    const f = evt.target;
    if (f && f.getAttribute && f.getAttribute("hx-post")) {
      evt.preventDefault();
      toast("Read-only demo — write actions are disabled.");
    }
  }, true);
})();
</script>
"""
    html = html.replace("</body>", shim + "</body>", 1)

    # 5. Per-pass metadata + client-side filter engine.
    meta_json = json.dumps(passes_meta, separators=(",", ":"))
    filter_engine = (
        '<script>window.PASSES = ' + meta_json + ';</script>\n' + FILTER_ENGINE_JS
    )
    html = html.replace("</body>", filter_engine + "</body>", 1)

    return html


# Client-side filter engine. Intercepts htmx GETs to /passes (or /status-badge)
# and replaces them with a local recompute over window.PASSES. Re-runs on
# DOMContentLoaded once so restored localStorage state takes effect.
FILTER_ENGINE_JS = r"""<script>
(function () {
  const SLOTS_PER_DAY = 32;
  const TOTAL_SLOTS = 224;

  function readState() {
    const f = document.getElementById("filter-form");
    const dir = f?.querySelector('[name="direction"]')?.value || "";
    const alertsOnly = !!f?.querySelector('[name="alerts_only"]')?.checked;
    const buckets = new Set(
      Array.from(document.querySelectorAll('#histogram input[name="buckets"]:checked'))
        .map((c) => parseInt(c.value, 10))
    );
    const totalBuckets = document.querySelectorAll('#histogram input[name="buckets"]').length;
    const allBuckets = buckets.size === 0 || buckets.size === totalBuckets;
    const maskInput = document.getElementById("time-mask");
    const maskStr = maskInput ? maskInput.value : "";
    const slots = maskStr.length === TOTAL_SLOTS
      ? Array.from(maskStr, (c) => c === "1")
      : new Array(TOTAL_SLOTS).fill(true);
    const allSlots = slots.every((b) => b);
    return { dir, alertsOnly, buckets, allBuckets, slots, allSlots };
  }

  function applyFilters() {
    const st = readState();
    const PASSES = window.PASSES || {};
    const bucketCounts = new Array(11).fill(0);
    const slotCount = new Array(TOTAL_SLOTS).fill(0);
    const slotSum = new Array(TOTAL_SLOTS).fill(0);
    const slotValid = new Array(TOTAL_SLOTS).fill(0);
    const slotTop = new Array(TOTAL_SLOTS).fill(null);
    let visible = 0;

    document.querySelectorAll('#pass-list article.pass-row').forEach((row) => {
      const id = row.id.replace('pass-', '');
      const m = PASSES[id];
      if (!m) { row.style.display = ''; return; }
      let show = true;
      if (st.dir && m.d !== st.dir) show = false;
      if (st.alertsOnly && !m.a) show = false;
      if (!st.allBuckets && (m.b == null || !st.buckets.has(m.b))) show = false;
      if (!st.allSlots && (m.s == null || !st.slots[m.s])) show = false;
      row.style.display = show ? '' : 'none';
      if (show) {
        visible++;
        if (m.b != null) bucketCounts[m.b]++;
        if (m.s != null) {
          slotCount[m.s]++;
          if (m.m != null) {
            slotSum[m.s] += m.m;
            slotValid[m.s]++;
            if (slotTop[m.s] == null || m.m > slotTop[m.s]) slotTop[m.s] = m.m;
          }
        }
      }
    });

    updateHistogram(bucketCounts, visible);
    updateHeatmap(slotCount, slotSum, slotValid, slotTop);
    updateEmptyState(visible);
  }

  function updateHistogram(counts, total) {
    const max = Math.max.apply(null, counts) || 1;
    const wraps = document.querySelectorAll('#histogram .bar-wrap');
    wraps.forEach((wrap, idx) => {
      const c = counts[idx] || 0;
      const pct = Math.round((c / max) * 100);
      const bar = wrap.querySelector('.bar');
      const lbl = wrap.querySelector('.bar-count');
      if (bar) bar.style.height = pct + '%';
      if (lbl) lbl.textContent = c ? String(c) : '';
      const baseTitle = (wrap.title || '').split(':')[0];
      if (baseTitle) wrap.title = baseTitle + ': ' + c;
    });
    const titleSpan = document.querySelector('#histogram .histogram-title .muted');
    if (titleSpan) titleSpan.textContent = '(' + total + ' pass' + (total === 1 ? '' : 'es') + ')';
  }

  function heatClass(c, max) {
    if (c <= 0 || max <= 0) return 0;
    const ratio = Math.sqrt(c / max);
    return Math.min(5, Math.max(1, Math.floor(ratio * 5 + 0.999)));
  }

  function heatClassSpeed(avg, minA, maxA) {
    if (avg == null || avg <= 0 || maxA <= 0) return 0;
    if (maxA <= minA) return 3;
    const ratio = (avg - minA) / (maxA - minA);
    return Math.min(5, Math.max(1, Math.floor(ratio * 5 + 0.999)));
  }

  function updateHeatmap(counts, sums, valid, tops) {
    const maxCount = Math.max.apply(null, counts) || 0;
    const avgs = counts.map((_, i) => valid[i] > 0 ? sums[i] / valid[i] : null);
    const validAvgs = avgs.filter((a) => a != null);
    const minAvg = validAvgs.length ? Math.min.apply(null, validAvgs) : 0;
    const maxAvg = validAvgs.length ? Math.max.apply(null, validAvgs) : 0;
    const mode = document.getElementById('heatmap')?.classList.contains('mode-speed') ? 'speed' : 'count';

    document.querySelectorAll('#heatmap .heatmap-cell').forEach((cell) => {
      const i = parseInt(cell.dataset.index, 10);
      const c = counts[i] || 0;
      const hC = heatClass(c, maxCount);
      const hS = heatClassSpeed(avgs[i], minAvg, maxAvg);
      cell.dataset.heatCount = String(hC);
      cell.dataset.heatSpeed = String(hS);
      cell.dataset.count = String(c);
      const heat = mode === 'speed' ? hS : hC;
      cell.classList.remove('heat-0', 'heat-1', 'heat-2', 'heat-3', 'heat-4', 'heat-5');
      cell.classList.add('heat-' + heat);
    });
  }

  function updateEmptyState(visible) {
    const list = document.getElementById('pass-list');
    if (!list) return;
    let empty = list.querySelector('.empty');
    if (visible === 0) {
      if (!empty) {
        empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = 'No passes match these filters.';
        list.appendChild(empty);
      }
      empty.style.display = '';
    } else if (empty) {
      empty.style.display = 'none';
    }
  }

  // Intercept htmx GETs to /passes and /status-badge — they have no live
  // backend, so just run the local filter (or do nothing for the badge).
  document.body.addEventListener('htmx:beforeRequest', function (evt) {
    const path = evt.detail?.requestConfig?.path || '';
    if (path.startsWith('/passes') || path.startsWith('/status-badge')) {
      evt.preventDefault();
      if (path.startsWith('/passes')) applyFilters();
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    // Slight delay so the upstream DOMContentLoaded handlers (which restore
    // localStorage state into the form / histogram / heatmap inputs) run first.
    setTimeout(applyFilters, 0);
  });
})();
</script>
"""


if __name__ == "__main__":
    main()
