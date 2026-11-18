/* paper2ink web UI.
 *
 * Three jobs: take dropped PDFs, poll their conversions, and show the
 * result either as the finished book or as a page-by-page comparison
 * against the original PDF.  No framework, no build step, no CDN.
 */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
    jobs: new Map(),
    polling: null,
    review: null,     // the review payload of the open job
    page: 0,
};

/* ------------------------------------------------------------ upload */

function options() {
    return {
        parser: $("opt-parser").value,
        dpi: $("opt-dpi").value,
        tables: $("opt-tables").value,
        color: $("opt-color").checked,
        cover: $("opt-cover").checked,
    };
}

async function upload(files) {
    const opts = options();
    for (const file of files) {
        if (!/\.pdf$/i.test(file.name)) continue;
        const body = new FormData();
        body.append("file", file);
        for (const [key, value] of Object.entries(opts)) body.append(key, value);
        try {
            const response = await fetch("/api/jobs", { method: "POST", body });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "upload failed");
            state.jobs.set(data.id, data);
        } catch (error) {
            state.jobs.set("err-" + Math.random().toString(36).slice(2), {
                id: null, name: file.name, status: "failed",
                error: String(error.message || error),
            });
        }
    }
    renderJobs();
    startPolling();
}

const zone = $("dropzone");
["dragenter", "dragover"].forEach((event) =>
    zone.addEventListener(event, (e) => {
        e.preventDefault();
        zone.classList.add("hot");
    })
);
["dragleave", "drop"].forEach((event) =>
    zone.addEventListener(event, (e) => {
        e.preventDefault();
        zone.classList.remove("hot");
    })
);
zone.addEventListener("drop", (e) => upload(e.dataTransfer.files));
$("pick").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => {
    upload(e.target.files);
    e.target.value = "";
});

/* ------------------------------------------------------- restore state */

/* Jobs live on the server, so a page reload must not lose them. */
async function loadJobs() {
    try {
        const response = await fetch("/api/jobs");
        if (!response.ok) return;
        const data = await response.json();
        for (const job of data.jobs) state.jobs.set(job.id, job);
        renderJobs();
        startPolling();
    } catch (_) { /* first run, nothing to restore */ }
}

loadJobs();

/* ------------------------------------------------------------ polling */

function startPolling() {
    if (state.polling) return;
    state.polling = setInterval(async () => {
        const pending = [...state.jobs.values()].filter(
            (job) => job.id && (job.status === "queued" || job.status === "running")
        );
        if (!pending.length) {
            clearInterval(state.polling);
            state.polling = null;
            return;
        }
        await Promise.all(
            pending.map(async (job) => {
                try {
                    const response = await fetch(`/api/jobs/${job.id}`);
                    if (response.ok) state.jobs.set(job.id, await response.json());
                } catch (_) { /* keep the old state and try again */ }
            })
        );
        renderJobs();
    }, 1200);
}

/* ------------------------------------------------------------ job list */

function renderJobs() {
    const host = $("jobs");
    host.innerHTML = "";
    for (const job of state.jobs.values()) host.appendChild(jobCard(job));
}

function jobCard(job) {
    const card = document.createElement("div");
    card.className = "job";

    const top = document.createElement("div");
    top.className = "job-top";
    top.innerHTML =
        `<span class="job-name"></span>` +
        `<span class="badge ${job.status}">${job.status}</span>`;
    top.querySelector(".job-name").textContent = job.name;
    card.appendChild(top);

    if (job.status === "queued" || job.status === "running") {
        const note = document.createElement("p");
        note.className = "note";
        note.textContent = job.message || "";
        card.appendChild(note);
        const bar = document.createElement("div");
        bar.className = "bar";
        bar.innerHTML = "<i></i>";
        card.appendChild(bar);
    }

    if (job.error) {
        const err = document.createElement("p");
        err.className = "note err";
        err.textContent = job.error;
        card.appendChild(err);
    }

    if (job.status === "done" && job.build) {
        const stats = document.createElement("p");
        stats.className = "stats";
        const entries = [
            ["sections", job.build.chapters],
            ["figures", job.build.figures],
            ["tables", job.build.tables],
            ["links", job.build.links],
            ["references", job.build.references],
        ];
        if (job.build.unresolved) entries.push(["unresolved", job.build.unresolved]);
        if (job.suspicious) entries.push(["to check", job.suspicious]);
        stats.innerHTML = entries
            .map(([label, value]) => `<span><b>${value}</b> ${label}</span>`)
            .join("");
        card.appendChild(stats);

        const validation = job.validation;
        if (validation && !validation.ok) {
            const bad = document.createElement("p");
            bad.className = "note err";
            bad.textContent = `${validation.errors.length} validation error(s): ` +
                validation.errors.slice(0, 2).join("; ");
            card.appendChild(bad);
        } else if (validation && validation.epubcheck === null) {
            const hint = document.createElement("p");
            hint.className = "note warn";
            hint.textContent = "Built, but EPUBCheck is not installed, so it was not verified.";
            card.appendChild(hint);
        }

        const actions = document.createElement("div");
        actions.className = "job-actions";

        const review = document.createElement("button");
        review.textContent = job.suspicious
            ? `Review (${job.suspicious} to check)`
            : "Review";
        review.addEventListener("click", () => openOverlay(job, "review"));
        actions.appendChild(review);

        const read = document.createElement("button");
        read.textContent = "Read";
        read.addEventListener("click", () => openOverlay(job, "read"));
        actions.appendChild(read);

        const download = document.createElement("button");
        download.textContent = "Download EPUB";
        download.addEventListener("click", () => {
            window.location.href = `/api/jobs/${job.id}/epub`;
        });
        actions.appendChild(download);

        const remove = document.createElement("button");
        remove.textContent = "Remove";
        remove.addEventListener("click", async () => {
            await fetch(`/api/jobs/${job.id}`, { method: "DELETE" });
            state.jobs.delete(job.id);
            renderJobs();
        });
        actions.appendChild(remove);

        card.appendChild(actions);
    }

    return card;
}

/* ------------------------------------------------------------ overlay */

const overlay = $("overlay");

$("ov-close").addEventListener("click", closeOverlay);
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.hidden) closeOverlay();
});

document.querySelectorAll(".tabs button").forEach((button) =>
    button.addEventListener("click", () => showTab(button.dataset.tab))
);

function showTab(name) {
    document.querySelectorAll(".tabs button").forEach((button) =>
        button.classList.toggle("active", button.dataset.tab === name)
    );
    $("tab-review").hidden = name !== "review";
    $("tab-read").hidden = name !== "read";
}

function closeOverlay() {
    overlay.hidden = true;
    $("book").src = "about:blank";
    state.review = null;
}

async function openOverlay(job, tab) {
    overlay.hidden = false;
    $("ov-title").textContent = job.name;
    showTab(tab);
    state.jobId = job.id;
    await Promise.all([loadReview(job.id), loadChapters(job.id)]);
}

/* ------------------------------------------------------------ reader */

async function loadChapters(jobId) {
    const list = $("chapters");
    list.innerHTML = "";
    let data;
    try {
        const response = await fetch(`/api/jobs/${jobId}/reader`);
        if (!response.ok) return;
        data = await response.json();
    } catch (_) {
        return;
    }
    data.chapters.forEach((chapter, index) => {
        const item = document.createElement("li");
        item.textContent = chapter.title;
        item.addEventListener("click", () => {
            list.querySelectorAll("li").forEach((li) => li.classList.remove("active"));
            item.classList.add("active");
            $("book").src = `/api/jobs/${jobId}/book/${chapter.href}`;
        });
        if (index === 0) item.classList.add("active");
        list.appendChild(item);
    });
    if (data.chapters.length) {
        $("book").src = `/api/jobs/${jobId}/book/${data.chapters[0].href}`;
    }
}

/* ------------------------------------------------------------ review */

async function loadReview(jobId) {
    let data;
    try {
        const response = await fetch(`/api/jobs/${jobId}/review`);
        if (!response.ok) return;
        data = await response.json();
    } catch (_) {
        return;
    }
    state.review = data;
    state.corrections = {
        removed: [], restored: [], regions: [], text_blocks: [], order: {},
        ...(data.corrections || {}),
    };
    // The server keys reordering by page number; JSON turned those into
    // strings on the way out.
    state.corrections.order = Object.fromEntries(
        Object.entries(state.corrections.order || {}).map(([page, ids]) => [
            Number(page), ids,
        ])
    );

    renderFindings(data);

    const byPage = new Map(data.pages.map((page) => [page.number, page]));
    const list = $("pagenums");
    list.innerHTML = "";
    for (let number = 0; number < data.page_count; number++) {
        const page = byPage.get(number);
        const flagged = page
            ? page.dropped.filter((drop) => drop.suspicious).length
            : 0;
        const item = document.createElement("li");
        item.innerHTML = `<span>Page ${number + 1}</span>`;
        if (flagged) {
            const flag = document.createElement("span");
            flag.className = "flag";
            flag.textContent = `${flagged}`;
            flag.title = `${flagged} dropped passage(s) worth checking`;
            item.appendChild(flag);
        }
        item.addEventListener("click", () => showPage(number));
        list.appendChild(item);
    }
    showPage(0);
}

function renderFindings(data) {
    const host = $("findings");
    host.innerHTML = "<h3>Findings</h3>";
    const add = (html, className) => {
        const p = document.createElement("p");
        if (className) p.className = className;
        p.innerHTML = html;
        host.appendChild(p);
        return p;
    };

    if (!data.suspicious.length) {
        add("No dropped text looks like prose.", "good");
    } else {
        add(`${data.suspicious.length} dropped passage(s) worth checking:`);
        data.suspicious.slice(0, 8).forEach((drop) => {
            const p = add("", "flagged");
            p.textContent = `p${drop.page + 1}: ${drop.text.slice(0, 70)}`;
            p.title = drop.text;
            p.addEventListener("click", () => showPage(drop.page));
        });
    }

    const unresolved = Object.entries(data.unresolved || {});
    if (unresolved.length) {
        const total = unresolved.reduce((sum, [, n]) => sum + n, 0);
        add(
            `${total} reference(s) point nowhere &mdash; usually supplementary ` +
            `figures in a separate file: ` +
            unresolved.slice(0, 6).map(([key]) => key).join(", ")
        );
    }
    (data.warnings || []).slice(0, 4).forEach((warning) => add(warning));
}

function showPage(number) {
    const data = state.review;
    if (!data) return;
    state.page = number;

    $("pagelabel").textContent = String(number + 1);
    document.querySelectorAll("#pagenums li").forEach((item, index) =>
        item.classList.toggle("active", index === number)
    );

    const image = $("pageimg");
    image.src = `/api/jobs/${state.jobId}/page/${number}`;
    image.onload = () => drawBoxes(number);

    // refreshEdits paints the block list and the overlay together, so the
    // pending corrections are always reflected in both.
    refreshEdits();
}

function drawBoxes(number) {
    const data = state.review;
    const image = $("pageimg");
    const host = $("boxes");
    host.innerHTML = "";
    const size = (data.page_sizes || [])[number];
    if (!size || !image.width) return;

    host.style.width = image.width + "px";
    host.style.height = image.height + "px";
    host.style.left = image.offsetLeft + "px";
    host.style.top = image.offsetTop + "px";

    const scaleX = image.width / size[0];
    const scaleY = image.height / size[1];
    // Remembered so a rectangle drawn in screen pixels can be turned back
    // into the PDF points the cropper works in.
    state.scale = { x: scaleX, y: scaleY, width: image.width, height: image.height };
    const page = data.pages.find((candidate) => candidate.number === number);
    if (!page) return;

    const place = (bbox, className, title, key) => {
        if (!bbox) return;
        const box = document.createElement("div");
        box.className = "bx " + className;
        box.style.left = bbox[0] * scaleX + "px";
        box.style.top = bbox[1] * scaleY + "px";
        box.style.width = Math.max(2, (bbox[2] - bbox[0]) * scaleX) + "px";
        box.style.height = Math.max(2, (bbox[3] - bbox[1]) * scaleY) + "px";
        box.title = title;
        if (key !== undefined) {
            box.dataset.key = key;
            box.addEventListener("mouseenter", () => highlight(key, true));
            box.addEventListener("mouseleave", () => highlight(key, false));
            box.addEventListener("click", () => select(key));
            if (state.selected === key) box.classList.add("hot");
        }
        host.appendChild(box);
    };

    page.assets.forEach((asset) => place(asset.bbox, "asset", asset.label));
    corrections().regions
        .filter((region) => region.page === number)
        .forEach((region, index) =>
            place(region.bbox, "drawn", `drawn: ${region.caption || "figure"}`,
                  "r" + index)
        );
    corrections().text_blocks
        .filter((picked) => picked.page === number)
        .forEach((picked, index) =>
            place(picked.bbox, "added", `picked up: ${picked.text.slice(0, 60)}`,
                  "t" + index)
        );
    page.kept.forEach((block, index) =>
        place(block.bbox, "kept", `${block.type}: ${block.text.slice(0, 80)}`, "k" + index)
    );
    page.dropped.forEach((drop, index) =>
        place(
            drop.bbox,
            "dropped" + (drop.suspicious ? " suspicious" : ""),
            `dropped (${drop.reason}): ${drop.text.slice(0, 80)}`,
            "d" + index
        )
    );
}

function highlight(key, on) {
    const element = document.querySelector(`#extracted [data-key="${key}"]`);
    if (element) {
        element.classList.toggle("hot", on);
        if (on) element.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
}

/* Clicking a block in either pane selects it in both, so there is always a
 * visible answer to "which one did I just press?". */
function select(key) {
    state.selected = state.selected === key ? null : key;
    document
        .querySelectorAll("#extracted .blk.hot, #boxes .bx.hot")
        .forEach((element) => element.classList.remove("hot"));
    if (!state.selected) return;
    document
        .querySelectorAll(`[data-key="${state.selected}"]`)
        .forEach((element) => {
            element.classList.add("hot");
            if (element.closest("#extracted")) {
                element.scrollIntoView({ block: "nearest", behavior: "smooth" });
            }
        });
}

function renderExtracted(page) {
    const host = $("extracted");
    host.innerHTML = "";
    if (!page || (!page.kept.length && !page.dropped.length)) {
        host.innerHTML = '<p class="empty">Nothing was extracted from this page.</p>';
        return;
    }
    const edits = corrections();

    page.kept.forEach((block, index) => {
        const div = document.createElement("div");
        div.className = "blk";
        if (edits.removed.includes(block.id)) div.classList.add("marked-out");
        if ((edits.order[page.number] || []).length) div.classList.add("moved");
        div.dataset.key = "k" + index;
        const kind = document.createElement("div");
        kind.className = "kind";
        kind.textContent = block.level ? `${block.type} ${block.level}` : block.type;
        const text = document.createElement("p");
        text.textContent = block.text;
        div.append(kind, text);
        if (state.selected === div.dataset.key) div.classList.add("hot");
        div.addEventListener("click", (event) => {
            if (!event.target.closest("button")) select(div.dataset.key);
        });

        const actions = document.createElement("div");
        actions.className = "blk-actions";
        const out = edits.removed.includes(block.id);
        actions.appendChild(
            makeButton(out ? "Keep after all" : "Leave out", () => {
                toggle(edits.removed, block.id);
                refreshEdits();
            })
        );
        actions.appendChild(
            makeButton("↑", () => movePageBlock(page, index, -1), "Read earlier")
        );
        actions.appendChild(
            makeButton("↓", () => movePageBlock(page, index, 1), "Read later")
        );
        div.appendChild(actions);
        host.appendChild(div);
    });

    page.dropped.forEach((drop, index) => {
        const div = document.createElement("div");
        div.className = "blk drop";
        if (edits.restored.includes(drop.id)) div.classList.add("marked-in");
        div.dataset.key = "d" + index;
        const kind = document.createElement("div");
        kind.className = "kind";
        kind.textContent = drop.suspicious
            ? `dropped — ${drop.reason} — check this`
            : `dropped — ${drop.reason}`;
        const text = document.createElement("p");
        text.textContent = drop.text;
        div.append(kind, text);
        if (state.selected === div.dataset.key) div.classList.add("hot");
        div.addEventListener("click", (event) => {
            if (!event.target.closest("button")) select(div.dataset.key);
        });

        const actions = document.createElement("div");
        actions.className = "blk-actions";
        const back = edits.restored.includes(drop.id);
        actions.appendChild(
            makeButton(back ? "Leave out again" : "Bring into the text", () => {
                toggle(edits.restored, drop.id);
                refreshEdits();
            })
        );
        div.appendChild(actions);
        host.appendChild(div);
    });
}

/* ------------------------------------------------- correcting the parse */

/* The corrections are a plain overlay on the parse: the server replays
 * them against the untouched parse on every rebuild, so nothing here
 * compounds and "discard" really goes back to what the parser said. */
function corrections() {
    if (!state.corrections) {
        state.corrections = {
            removed: [], restored: [], regions: [], text_blocks: [], order: {},
        };
    }
    return state.corrections;
}

function toggle(list, value) {
    const at = list.indexOf(value);
    if (at === -1) list.push(value);
    else list.splice(at, 1);
}

function countEdits() {
    const edits = corrections();
    return (
        edits.removed.length +
        edits.restored.length +
        edits.regions.length +
        edits.text_blocks.length +
        Object.keys(edits.order).length
    );
}

function refreshEdits() {
    const total = countEdits();
    const edits = corrections();
    const parts = [];
    if (edits.removed.length) parts.push(`${edits.removed.length} left out`);
    if (edits.restored.length) parts.push(`${edits.restored.length} brought back`);
    if (edits.regions.length) parts.push(`${edits.regions.length} drawn`);
    if (edits.text_blocks.length)
        parts.push(`${edits.text_blocks.length} text picked up`);
    if (Object.keys(edits.order).length)
        parts.push(`${Object.keys(edits.order).length} page(s) reordered`);
    $("edit-summary").textContent = total ? parts.join(", ") : "no changes";
    $("rebuild").disabled = total === 0;
    $("reset-corrections").disabled = total === 0;

    // Redrawing the list would otherwise throw the reader back to the top
    // of the page, which reads as "nothing happened".
    const list = $("extracted");
    const offset = list.scrollTop;
    const page = state.review.pages.find((p) => p.number === state.page);
    renderExtracted(page);
    list.scrollTop = offset;
    drawBoxes(state.page);
}

function movePageBlock(page, index, direction) {
    const edits = corrections();
    const ids = (edits.order[page.number] || page.kept.map((block) => block.id)).slice();
    const target = index + direction;
    if (target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    edits.order[page.number] = ids;
    // Mirror the move in the data we are showing, so the list reorders too.
    const moved = page.kept.splice(index, 1)[0];
    page.kept.splice(target, 0, moved);
    refreshEdits();
}

$("reset-corrections").addEventListener("click", () => {
    state.corrections = {
        removed: [], restored: [], regions: [], text_blocks: [], order: {},
    };
    loadReview(state.jobId);
});

$("rebuild").addEventListener("click", async () => {
    const button = $("rebuild");
    button.disabled = true;
    $("edit-summary").textContent = "rebuilding…";
    try {
        await postJSON(`/api/jobs/${state.jobId}/rebuild`, {
            corrections: corrections(),
        });
    } catch (error) {
        $("edit-summary").textContent = String(error.message || error);
        button.disabled = false;
        return;
    }
    await waitForJob(state.jobId);
    await loadReview(state.jobId);
    await loadChapters(state.jobId);
    loadShelf();
});

async function waitForJob(jobId) {
    for (let tries = 0; tries < 900; tries++) {
        let job;
        try {
            job = await (await fetch(`/api/jobs/${jobId}`)).json();
        } catch (_) {
            return;
        }
        if (job.status === "done" || job.status === "failed") {
            state.jobs.set(job.id, job);
            renderJobs();
            if (job.status === "failed") {
                $("edit-summary").textContent = job.error || "rebuild failed";
            }
            return;
        }
        $("edit-summary").textContent = job.message || "working…";
        await new Promise((resolve) => setTimeout(resolve, 400));
    }
}

/* ------------------------------------------------- drawing a region */

const drawToggle = $("draw-toggle");
const textToggle = $("text-toggle");
/* "" | "figure" | "text" - only one mode can be armed at a time. */
let drawMode = "";

function setDrawMode(mode) {
    drawMode = drawMode === mode ? "" : mode;
    drawToggle.classList.toggle("on", drawMode === "figure");
    textToggle.classList.toggle("on", drawMode === "text");
    $("pagebox").classList.toggle("drawing", drawMode !== "");
    drawToggle.textContent =
        drawMode === "figure" ? "Stop drawing" : "Draw a figure region";
    textToggle.textContent = drawMode === "text" ? "Stop picking" : "Pick up text";
}

drawToggle.addEventListener("click", () => setDrawMode("figure"));
textToggle.addEventListener("click", () => setDrawMode("text"));

/* Reading the text out of the rectangle happens on the server, which has
 * the PDF.  The reader gets it back to check and correct first: a clip can
 * catch a stray column, and silently inserting whatever came out would be
 * worse than not offering this at all. */
async function pickUpText(bbox) {
    let found;
    try {
        found = await postJSON(`/api/jobs/${state.jobId}/text-in-region`, {
            page: state.page,
            bbox,
        });
    } catch (error) {
        alert(String(error.message || error));
        return;
    }
    if (found.empty) {
        alert("There is no text in that rectangle.");
        return;
    }
    const text = prompt(
        "Insert this text as a block? You can correct it here.",
        found.text
    );
    if (text === null || !text.trim()) return;

    const type = /^\s*\d+(\.\d+)*\.?\s+\S/.test(text) && text.length < 90
        ? "heading"
        : "paragraph";
    corrections().text_blocks.push({ page: state.page, bbox, text, type });
    refreshEdits();
}

(function enableDrawing() {
    const box = $("pagebox");
    let start = null;
    let rubber = null;

    const pointIn = (event) => {
        const image = $("pageimg");
        const rect = image.getBoundingClientRect();
        return {
            x: Math.min(Math.max(event.clientX - rect.left, 0), rect.width),
            y: Math.min(Math.max(event.clientY - rect.top, 0), rect.height),
        };
    };

    // Cancelling mousedown is not enough on its own: the browser still
    // takes the page image with the pointer as a native drag, so dragstart
    // has to be refused outright.
    box.addEventListener("dragstart", (event) => event.preventDefault());

    box.addEventListener("mousedown", (event) => {
        if (!drawMode || event.button !== 0) return;
        event.preventDefault();
        start = pointIn(event);
        rubber = document.createElement("div");
        rubber.className = drawMode === "text" ? "rubber text" : "rubber";
        $("boxes").appendChild(rubber);
    });

    box.addEventListener("mousemove", (event) => {
        if (!start || !rubber) return;
        const now = pointIn(event);
        rubber.style.left = Math.min(start.x, now.x) + "px";
        rubber.style.top = Math.min(start.y, now.y) + "px";
        rubber.style.width = Math.abs(now.x - start.x) + "px";
        rubber.style.height = Math.abs(now.y - start.y) + "px";
    });

    box.addEventListener("mouseup", (event) => {
        if (!start || !rubber) return;
        const end = pointIn(event);
        const from = start;
        start = null;
        rubber.remove();
        rubber = null;

        const scale = state.scale;
        if (!scale) return;
        const width = Math.abs(end.x - from.x);
        const height = Math.abs(end.y - from.y);
        if (width < 12 || height < 12) return;   // a stray click, not a drag

        const bbox = [
            Math.min(from.x, end.x) / scale.x,
            Math.min(from.y, end.y) / scale.y,
            Math.max(from.x, end.x) / scale.x,
            Math.max(from.y, end.y) / scale.y,
        ];

        if (drawMode === "text") {
            pickUpText(bbox);
            return;
        }

        const caption = prompt(
            "Caption for this figure (leave empty for none).\n" +
            "Start with “Figure 7” or “Table 2” to give it that number."
        );
        if (caption === null) return;

        const label = /^\s*(figure|fig\.?|table|scheme)\s*\d/i.test(caption)
            ? caption
            : "";
        corrections().regions.push({
            page: state.page,
            bbox,
            label,
            caption: label ? caption.replace(/^[^.:|]*[.:|]\s*/, "") : caption,
            kind: /^\s*table/i.test(caption) ? "table" : "figure",
        });
        refreshEdits();
    });
})();

/* ------------------------------------------------------------ settings */

const settingsForm = $("settings");

$("settings-toggle").addEventListener("click", () => {
    settingsForm.hidden = !settingsForm.hidden;
    if (!settingsForm.hidden) loadSettings();
});

async function loadSettings() {
    try {
        const data = await (await fetch("/api/settings")).json();
        $("set-email").value = data.kindle_email || "";
        $("set-output").value = data.output_dir || "";
        $("set-parser").value = data.parser;
        $("set-dpi").value = String(data.dpi);
        $("set-validate").checked = !!data.validate;
        $("set-path").textContent = `Stored in ${data.path}`;
        $("set-output-note").textContent = data.output_exists
            ? "The finished EPUBs are written here."
            : "This folder will be created when you save.";
        state.settings = data;
    } catch (_) { /* leave the form as it is */ }
}

settingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = $("set-status");
    status.textContent = "saving…";
    try {
        const response = await fetch("/api/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                kindle_email: $("set-email").value.trim(),
                output_dir: $("set-output").value.trim(),
                parser: $("set-parser").value,
                dpi: Number($("set-dpi").value),
                validate: $("set-validate").checked,
            }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "could not save");
        status.textContent = "saved";
        state.settings = data;
        $("opt-parser").value = data.parser;
        $("opt-dpi").value = String(data.dpi);
        setTimeout(() => (status.textContent = ""), 2500);
    } catch (error) {
        status.textContent = String(error.message || error);
    }
});

/* ------------------------------------------------------------ the shelf */

const filters = {
    search: $("f-search"),
    sent: $("f-sent"),
    parser: $("f-parser"),
    year: $("f-year"),
    sort: $("f-sort"),
};
let descending = true;

Object.values(filters).forEach((element) =>
    element.addEventListener("input", () => loadShelf())
);
$("f-order").addEventListener("click", () => {
    descending = !descending;
    $("f-order").textContent = descending ? "↓" : "↑";
    loadShelf();
});

async function loadShelf() {
    const params = new URLSearchParams({
        search: filters.search.value.trim(),
        sent: filters.sent.value,
        parser: filters.parser.value,
        year: filters.year.value,
        sort: filters.sort.value,
        order: descending ? "desc" : "asc",
    });
    let data;
    try {
        data = await (await fetch("/api/library?" + params)).json();
    } catch (_) {
        return;
    }

    const stats = data.stats;
    $("shelf-stats").textContent = stats.total
        ? `${stats.total} converted, ${stats.unsent} not sent yet`
        : "nothing converted yet";

    fillOptions(filters.parser, data.parsers, "any parser");
    fillOptions(filters.year, data.years, "any year");

    const host = $("shelf");
    host.innerHTML = "";
    if (!data.entries.length) {
        host.innerHTML = '<p class="empty">Nothing matches these filters.</p>';
        return;
    }
    data.entries.forEach((entry) => host.appendChild(shelfRow(entry)));
}

function fillOptions(select, values, placeholder) {
    const current = select.value;
    select.innerHTML = "";
    const first = document.createElement("option");
    first.value = "";
    first.textContent = placeholder;
    select.appendChild(first);
    values.forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
    });
    select.value = current;
}

function shelfRow(entry) {
    const row = document.createElement("div");
    row.className = "row " + (entry.is_sent ? "sent" : "unsent");

    const star = document.createElement("button");
    star.className = "star" + (entry.starred ? " on" : "");
    star.textContent = entry.starred ? "★" : "☆";
    star.title = entry.starred ? "Remove the star" : "Star this paper";
    star.addEventListener("click", async () => {
        await postJSON(`/api/library/${entry.id}/starred`, {
            starred: !entry.starred,
        });
        loadShelf();
    });
    row.appendChild(star);

    const main = document.createElement("div");
    main.className = "row-main";
    const title = document.createElement("div");
    title.className = "row-title";
    title.textContent = entry.title || entry.source_name;
    const meta = document.createElement("div");
    meta.className = "row-meta";
    const bits = [
        entry.first_author && entry.year
            ? `${entry.first_author} ${entry.year}`
            : entry.first_author || entry.year,
        entry.journal,
        entry.pages ? `${entry.pages} pages` : "",
        entry.figures ? `${entry.figures} figures` : "",
        entry.references_count ? `${entry.references_count} refs` : "",
        entry.parser,
    ].filter(Boolean);
    meta.textContent = bits.join(" · ");
    main.append(title, meta);
    row.appendChild(main);

    const pill = document.createElement("span");
    if (!entry.epub_exists) {
        pill.className = "pill gone";
        pill.textContent = "file missing";
    } else {
        pill.className = "pill " + (entry.is_sent ? "sent" : "unsent");
        pill.textContent = entry.is_sent
            ? "sent " + entry.sent_at.slice(0, 10)
            : "not sent";
    }
    row.appendChild(pill);

    const actions = document.createElement("div");
    actions.className = "row-actions";

    if (entry.epub_exists) {
        actions.appendChild(makeButton("Send to Kindle", () => sendToKindle(entry)));
        actions.appendChild(
            makeButton("Show file", () => postJSON(`/api/library/${entry.id}/reveal`, {}))
        );
        actions.appendChild(
            makeButton("Download", () => {
                window.location.href = `/api/library/${entry.id}/epub`;
            })
        );
    }
    actions.appendChild(
        makeButton(entry.is_sent ? "Mark unsent" : "Mark sent", async () => {
            await postJSON(`/api/library/${entry.id}/sent`, { sent: !entry.is_sent });
            loadShelf();
        })
    );
    actions.appendChild(
        makeButton("Remove", async () => {
            await fetch(`/api/library/${entry.id}`, { method: "DELETE" });
            loadShelf();
        })
    );
    row.appendChild(actions);
    return row;
}

function makeButton(label, handler, title) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (title) button.title = title;
    button.addEventListener("click", handler);
    return button;
}

async function postJSON(url, payload) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "request failed");
    return data;
}

/* A mailto: link cannot carry an attachment - every mail client refuses
 * that - so the draft opens pre-addressed and the folder holding the book
 * is revealed, leaving one drag to the user. */
async function sendToKindle(entry) {
    let data;
    try {
        data = await postJSON(`/api/library/${entry.id}/send`, {});
    } catch (error) {
        alert(String(error.message || error));
        if (settingsForm.hidden) $("settings-toggle").click();
        return;
    }
    window.location.href = data.mailto;
    const name = data.file.split(/[\\/]/).pop();
    setTimeout(async () => {
        const question =
            `Attach "${name}" from the folder that just opened, then send.` +
            `\n\nMark it as sent now?`;
        if (confirm(question)) {
            await postJSON(`/api/library/${entry.id}/sent`, { sent: true });
            loadShelf();
        }
    }, 800);
}

loadShelf();
loadSettings();
