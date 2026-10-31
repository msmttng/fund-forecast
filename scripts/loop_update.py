import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# 同ディレクトリの build_data をインポート可能にする
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_data  # noqa: E402
import triple_advisor  # noqa: E402

JST = build_data.JST


def determine_interval_sec(
    now_jst: datetime.datetime,
    base_interval: int,
    offhours_interval: int,
    elapsed_minutes: float,
    event_name: str,
) -> int:
    """現在時刻（JST）に応じて更新間隔（通常5分、土日・日米休場時間帯は30分）を返す。"""
    # 手動実行 (workflow_dispatch) の開始直後35分間は即時確認のため通常間隔で動作させる
    if event_name == "workflow_dispatch" and elapsed_minutes < 35.0:
        return base_interval

    wd = now_jst.weekday()  # 0=月 ... 5=土, 6=日
    hm = now_jst.hour * 60 + now_jst.minute

    # 日曜終日
    if wd == 6:
        return offhours_interval
    # 土曜 07:15 以降（米国金曜市場・大証夜間終了後）
    if wd == 5 and hm >= 7 * 60 + 15:
        return offhours_interval
    # 月曜 06:45 未満（週明け市場開始前）
    if wd == 0 and hm < 6 * 60 + 45:
        return offhours_interval
    # 日本の祝日・年末年始（平日）で米国市場も開いていない日中〜夜（07:15〜21:30 JST）
    if not build_data.is_jp_business_day(now_jst.date()) and (7 * 60 + 15 <= hm < 21 * 60 + 30):
        return offhours_interval
    # 平日の日米市場幕間（06:15〜07:45 JST：米国・大証夜間終了後〜東京開場前）
    if 6 * 60 + 15 <= hm < 7 * 60 + 45:
        return offhours_interval

    return base_interval


def run_git(args: List[str], input_bytes: Optional[bytes] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Git コマンドを UTF-8 / バイナリ安全に実行する。"""
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "github-actions[bot]")
    env.setdefault("GIT_AUTHOR_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com")
    env.setdefault("GIT_COMMITTER_NAME", "github-actions[bot]")
    env.setdefault("GIT_COMMITTER_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com")
    return subprocess.run(
        ["git"] + args,
        cwd=str(ROOT_DIR),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=check,
    )


def read_blob_from_fetch_head(rel_path: str) -> Optional[Dict[str, Any]]:
    """FETCH_HEAD 上の JSON ファイルを読み取って辞書として返す（存在しなければ None）。"""
    res = run_git(["show", f"FETCH_HEAD:{rel_path}"], check=False)
    if res.returncode != 0 or not res.stdout:
        return None
    try:
        obj = json.loads(res.stdout.decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def restore_state_from_data_branch(remote: str, branch: str) -> None:
    """起動時に既存の data ブランチから基準価額キャッシュ・答え合わせログ・日次確定値キャッシュを引き継ぐ。"""
    fetch_res = run_git(["fetch", "--depth=1", remote, branch], check=False)
    if fetch_res.returncode != 0:
        print(f"[INFO] Remote branch {remote}/{branch} does not exist yet (initial run).")
        return

    # 1. nav_cache.json の引き継ぎ（6時間以内のキャッシュ再利用をランナー交代後も維持）
    remote_nav = read_blob_from_fetch_head("nav_cache.json")
    if remote_nav:
        local_nav: Dict[str, Any] = {}
        if build_data.NAV_CACHE_PATH.exists():
            try:
                local_nav = json.loads(build_data.NAV_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                local_nav = {}
        merged_nav = dict(local_nav)
        for fid, r_entry in remote_nav.items():
            if not isinstance(r_entry, dict) or "navDate" not in r_entry:
                continue
            l_entry = merged_nav.get(fid)
            if not isinstance(l_entry, dict):
                merged_nav[fid] = r_entry
                continue
            r_date = str(r_entry.get("navDate", ""))
            l_date = str(l_entry.get("navDate", ""))
            r_fetched = str(r_entry.get("fetchedAt", ""))
            l_fetched = str(l_entry.get("fetchedAt", ""))
            if (r_date > l_date) or (r_date == l_date and r_fetched > l_fetched):
                merged_nav[fid] = r_entry
        build_data.NAV_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        build_data.NAV_CACHE_PATH.write_text(
            json.dumps(merged_nav, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"[INFO] Restored nav_cache.json ({len(merged_nav)} funds) from {remote}/{branch}.")

    # 2. forecast_log.json の引き継ぎ（毎朝 8:30 JST の live 記録を永続保持）
    remote_log = read_blob_from_fetch_head("forecast_log.json")
    if remote_log and isinstance(remote_log.get("entries"), dict):
        local_log: Dict[str, Any] = {"version": 1, "entries": {}}
        if build_data.FORECAST_LOG_PATH.exists():
            try:
                local_log = json.loads(build_data.FORECAST_LOG_PATH.read_text(encoding="utf-8"))
            except Exception:
                local_log = {"version": 1, "entries": {}}
        l_entries = local_log.setdefault("entries", {})
        for d_str, day_map in remote_log["entries"].items():
            if not isinstance(day_map, dict):
                continue
            bucket = l_entries.setdefault(d_str, {})
            for fid, r_rec in day_map.items():
                if not isinstance(r_rec, dict):
                    continue
                l_rec = bucket.get(fid)
                if l_rec is None:
                    bucket[fid] = r_rec
                elif r_rec.get("source") == "live" and l_rec.get("source") != "live":
                    bucket[fid] = r_rec
                elif r_rec.get("actualNav") is not None and l_rec.get("actualNav") is None:
                    bucket[fid] = r_rec
        local_log["entries"] = {k: l_entries[k] for k in sorted(l_entries.keys())}
        build_data.FORECAST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        build_data.FORECAST_LOG_PATH.write_text(
            json.dumps(local_log, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"[INFO] Restored forecast_log.json ({len(local_log['entries'])} days) from {remote}/{branch}.")

    # 3. market_anchor_cache.json の引き継ぎ（仲値・TOPIX過去日足・清算値キャッシュ）
    remote_anchor = read_blob_from_fetch_head("market_anchor_cache.json")
    if remote_anchor:
        build_data.save_anchor_cache(remote_anchor)
        print(f"[INFO] Restored market_anchor_cache.json from {remote}/{branch}.")

    # 4. triple_notify_state.json の引き継ぎ（Discord通知の重複送信防止ステート）
    remote_triple_state = read_blob_from_fetch_head("triple_notify_state.json")
    if remote_triple_state:
        triple_advisor.save_notify_state(remote_triple_state)
        print(f"[INFO] Restored triple_notify_state.json from {remote}/{branch}.")


def push_orphan_data_branch(
    data_json_path: pathlib.Path,
    remote: str,
    branch: str,
    now_jst: datetime.datetime,
    dry_run: bool = False,
) -> str:
    """main の作業ツリーを切り替えずに、履歴を持たない 1 コミットを作成して data ブランチへ force push する。"""
    files_to_pack: List[Tuple[str, pathlib.Path]] = [
        ("data.json", data_json_path),
        ("triple_data.json", triple_advisor.TRIPLE_DATA_JSON_PATH),
        ("triple_notify_state.json", triple_advisor.TRIPLE_NOTIFY_STATE_PATH),
        ("nav_cache.json", build_data.NAV_CACHE_PATH),
        ("forecast_log.json", build_data.FORECAST_LOG_PATH),
        ("market_anchor_cache.json", build_data.ANCHOR_CACHE_PATH),
    ]

    tree_lines: List[str] = []
    for target_name, src_path in files_to_pack:
        if not src_path.exists():
            continue
        blob_res = run_git(["hash-object", "-w", str(src_path)])
        blob_sha = blob_res.stdout.decode("utf-8").strip()
        tree_lines.append(f"100644 blob {blob_sha}\t{target_name}")

    mktree_input = ("\n".join(tree_lines) + "\n").encode("utf-8")
    tree_res = run_git(["mktree"], input_bytes=mktree_input)
    tree_sha = tree_res.stdout.decode("utf-8").strip()

    msg = f"Update forecast data ({now_jst.isoformat(timespec='seconds')})"
    # -p（親コミット）を指定しないことで、常に履歴1件のみの孤立コミットを生成する
    commit_res = run_git(["commit-tree", tree_sha, "-m", msg])
    commit_sha = commit_res.stdout.decode("utf-8").strip()

    if dry_run:
        print(f"[DRY-RUN] Created single-commit {commit_sha[:8]} for branch '{branch}' (push skipped).")
        return commit_sha

    last_err = ""
    for attempt in range(1, 4):
        push_res = run_git(["push", "--force", remote, f"{commit_sha}:refs/heads/{branch}"], check=False)
        if push_res.returncode == 0:
            print(f"[PUSH] Force-pushed {commit_sha[:8]} -> {remote}/{branch} at {now_jst.isoformat(timespec='seconds')}")
            return commit_sha
        last_err = push_res.stderr.decode("utf-8", errors="replace").strip()
        print(f"[WARN] git push attempt {attempt} failed: {last_err}", file=sys.stderr)
        time.sleep(3.0 * attempt)

    raise RuntimeError(f"Failed to force-push to {remote}/{branch}: {last_err}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuously update fund-forecast data to orphan 'data' branch")
    parser.add_argument("--interval", type=int, default=300, help="通常時の更新間隔（秒、デフォルト: 300 = 5分）")
    parser.add_argument("--offhours-interval", type=int, default=1800, help="休場時間帯の更新間隔（秒、デフォルト: 1800 = 30分）")
    parser.add_argument("--max-minutes", type=float, default=345.0, help="最大稼働時間（分、デフォルト: 345 = 5時間45分）")
    parser.add_argument("--nav-ttl-hours", type=float, default=6.0, help="基準価額CSVキャッシュの再利用時間（時間、デフォルト: 6）")
    parser.add_argument("--remote", type=str, default="origin", help="push 先リモート名")
    parser.add_argument("--branch", type=str, default="data", help="push 先ブランチ名")
    parser.add_argument("--dry-run", action="store_true", help="git push を行わずに1コミット生成まで検証する")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    start_mono = time.monotonic()
    deadline_mono = start_mono + args.max_minutes * 60.0

    print(
        f"[START] loop_update.py (interval={args.interval}s, offhours={args.offhours_interval}s, "
        f"max_minutes={args.max_minutes}, event={event_name or 'local'}, dry_run={args.dry_run})",
        flush=True,
    )

    if not args.dry_run:
        try:
            restore_state_from_data_branch(args.remote, args.branch)
        except Exception as e:
            print(f"[WARN] State restore skipped: {e}", file=sys.stderr, flush=True)

    iteration = 0
    while True:
        iteration += 1
        iter_start_mono = time.monotonic()
        elapsed_min = (iter_start_mono - start_mono) / 60.0
        now_jst = datetime.datetime.now(JST)

        current_interval = determine_interval_sec(
            now_jst=now_jst,
            base_interval=args.interval,
            offhours_interval=args.offhours_interval,
            elapsed_minutes=elapsed_min,
            event_name=event_name,
        )

        print(
            f"\n[LOOP #{iteration}] {now_jst.isoformat(timespec='seconds')} "
            f"(elapsed={elapsed_min:.1f}m / {args.max_minutes:.1f}m, next_interval={current_interval}s)",
            flush=True,
        )

        try:
            app_data = build_data.run_build(
                force_nav=False,
                nav_ttl_hours=args.nav_ttl_hours,
                next_update_in_sec=current_interval,
                output_path=build_data.DATA_JSON_PATH,
            )
            advisor_dt = datetime.datetime.now(JST)
            t_data = triple_advisor.build_triple_advisor_data(
                app_data=app_data,
                now_jst=advisor_dt,
                output_path=triple_advisor.TRIPLE_DATA_JSON_PATH,
            )
            force_discord_test = (
                iteration == 1 and os.environ.get("FORCE_DISCORD_TEST", "").strip().lower() == "true"
            )
            try:
                triple_advisor.evaluate_and_notify_discord(
                    triple_data=t_data,
                    now_jst=advisor_dt,
                    force_test=force_discord_test,
                    dry_run=args.dry_run,
                )
            except Exception as notify_err:
                print(f"[WARN] Discord notification failed: {notify_err}", file=sys.stderr, flush=True)

            push_dt = datetime.datetime.now(JST)
            push_orphan_data_branch(
                data_json_path=build_data.DATA_JSON_PATH,
                remote=args.remote,
                branch=args.branch,
                now_jst=push_dt,
                dry_run=args.dry_run,
            )
        except Exception as e:
            print(f"[ERROR] Loop #{iteration} failed: {e}", file=sys.stderr, flush=True)

        now_mono = time.monotonic()
        if now_mono >= deadline_mono:
            print("[DONE] Reached max-minutes deadline. Exiting cleanly.", flush=True)
            break

        iter_duration = now_mono - iter_start_mono
        sleep_sec = max(5.0, float(current_interval) - iter_duration)
        if now_mono + sleep_sec >= deadline_mono:
            print(
                f"[DONE] Next cycle ({sleep_sec:.0f}s) would exceed max-minutes ({args.max_minutes}m). Exiting cleanly.",
                flush=True,
            )
            break

        print(f"[SLEEP] Waiting {sleep_sec:.1f}s until next update...", flush=True)
        time.sleep(sleep_sec)


if __name__ == "__main__":
    main()
