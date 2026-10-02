"""Corners of the page's approval and outside-changes dialogs, in a real browser.

Known-good: an approval request that names no title still asks, as "Approve?",
and answering a request the server no longer holds closes the dialog and says so
in the transcript; the outside-changes dialog counts in the singular for one
file and the plural for several, offers Remove only when the session created
something, and says when a restore could not find a backup.

Known-bad: nothing offers Restore with nothing to restore, or a list of running
programs when none are.
"""

from __future__ import annotations

from pathlib import Path

from chrome_page import drive_page, served_chat


def test_an_untitled_approval_asks_approve_and_a_stale_answer_says_so(tmp_path: Path) -> None:
    with served_chat(tmp_path) as site:
        got = drive_page(
            site.base,
            """
            await page.chat(args.sid);
            await page.js(() => askApproval({ id: "gone" }));
            const shown = await page.js(() => ({
              open: document.querySelector("#approval-dialog").open,
              title: document.querySelector("#approval-title").textContent,
              lines: document.querySelector("#approval-lines").textContent,
            }));
            await page.click("#approval-approve");
            await page.until(() => [...document.querySelectorAll(".notice.error")]
              .some((n) => n.textContent.includes("no such approval request")));
            const closed = await page.js(() => !document.querySelector("#approval-dialog").open);
            const cleared = await page.js(() => state.approvalId === null);
            return { shown, closed, cleared };
            """,
            sid=site.sid,
        )
    assert got["shown"] == {"open": True, "title": "Approve?", "lines": ""}
    assert got["closed"] is True
    assert got["cleared"] is True


def test_the_outside_dialog_counts_and_offers_only_what_applies(tmp_path: Path) -> None:
    with served_chat(tmp_path) as site:
        got = drive_page(
            site.base,
            """
            await page.chat(args.sid);
            return await page.js(() => {
              const view = (extra) => ({
                files: [], downloads: [], packages: [], not_tracked: [], undone: [],
                limits: "", can_restore: 0, can_delete: 0, empty: false, ...extra });
              const read = () => ({
                restore: document.querySelector("#outside-restore").textContent,
                restoreOff: document.querySelector("#outside-restore").disabled,
                remove: document.querySelector("#outside-remove").textContent,
                removeHidden: document.querySelector("#outside-remove").hidden,
                confirm: document.querySelector("#outside-confirm-text").textContent,
                programs: document.querySelector("#outside-processes").textContent,
                programsHidden: document.querySelector("#outside-processes").hidden,
              });
              document.querySelector("#outside-dialog").showModal();
              paintOutside(view({ can_restore: 1, can_delete: 1, processes: [{}] }));
              const single = read();
              paintOutside(view({ can_restore: 2, can_delete: 3, processes: [{}, {}] }));
              const plural = read();
              paintOutside(view({}));
              const none = read();
              return { single, plural, none };
            });
            """,
            sid=site.sid,
        )
    assert got["single"]["restore"] == "Restore 1 file"
    assert got["single"]["remove"] == "Remove 1 created file…"
    assert got["single"]["confirm"].startswith("Remove the 1 file this session created?")
    assert got["single"]["programs"] == "1 program still running: open the list"
    assert got["plural"]["restore"] == "Restore 2 files"
    assert got["plural"]["remove"] == "Remove 3 created files…"
    assert got["plural"]["programs"] == "2 programs still running: open the list"
    assert got["none"]["restoreOff"] is True
    assert got["none"]["removeHidden"] is True
    assert got["none"]["programsHidden"] is True  # no processes field at all


def test_an_undo_that_misses_a_backup_says_so_and_a_failed_undo_says_why(tmp_path: Path) -> None:
    with served_chat(tmp_path) as site:
        got = drive_page(
            site.base,
            """
            await page.chat(args.sid);
            const notices = () => page.js(() => [...document.querySelectorAll(".notice")]
              .map((n) => n.className + "|" + n.textContent));
            await page.js(() => {
              const real = window.fetch;
              let calls = 0;
              window.fetch = (url, options) => {
                if (!String(url).endsWith("/outside/undo")) return real(url, options);
                calls += 1;
                const reply = calls === 1
                  ? { restored: ["a"], deleted: [], failed: ["b", "c"],
                      record: { files: [], downloads: [], packages: [], not_tracked: [],
                                undone: [], limits: "", can_restore: 0, can_delete: 0,
                                empty: true } }
                  : { error: "backup store is gone" };
                return Promise.resolve(new Response(JSON.stringify(reply),
                  { status: calls === 1 ? 200 : 500,
                    headers: { "Content-Type": "application/json" } }));
              };
              document.querySelector("#outside-dialog").showModal();
            });
            await page.js(() => document.querySelector("#outside-restore").click());
            await page.until(() => [...document.querySelectorAll(".notice")]
              .some((n) => n.textContent.startsWith("Undo: 1 restored")));
            await page.js(() => {
              document.querySelector("#outside-restore").disabled = false;
              document.querySelector("#outside-restore").click();
            });
            await page.until(() => [...document.querySelectorAll(".notice.error")]
              .some((n) => n.textContent.includes("backup store is gone")));
            return await notices();
            """,
            sid=site.sid,
        )
    assert any(
        n.startswith("notice |") and "Undo: 1 restored, 2 could not be restored (no backup)." in n
        for n in got
    )
    assert any(n.startswith("notice error|") and "backup store is gone" in n for n in got)
