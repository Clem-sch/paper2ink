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

    const page = data.pages.find((candidate) => candidate.number === number);
    renderExtracted(page);
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
            box.addEventListener("mouseenter", () => highlight(key, true));
            box.addEventListener("mouseleave", () => highlight(key, false));
        }
        host.appendChild(box);
    };

    page.assets.forEach((asset) => place(asset.bbox, "asset", asset.label));
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
    const element = document.querySelector(`[data-key="${key}"]`);
    if (element) {
        element.classList.toggle("hot", on);
        if (on) element.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
}

function renderExtracted(page) {
    const host = $("extracted");
    host.innerHTML = "";
    if (!page || (!page.kept.length && !page.dropped.length)) {
        host.innerHTML = '<p class="empty">Nothing was extracted from this page.</p>';
        return;
    }

    page.kept.forEach((block, index) => {
        const div = document.createElement("div");
        div.className = "blk";
        div.dataset.key = "k" + index;
        const kind = document.createElement("div");
        kind.className = "kind";
        kind.textContent = block.level ? `${block.type} ${block.level}` : block.type;
        const text = document.createElement("p");
        text.textContent = block.text;
        div.append(kind, text);
        host.appendChild(div);
    });

    page.dropped.forEach((drop, index) => {
        const div = document.createElement("div");
        div.className = "blk drop";
        div.dataset.key = "d" + index;
        const kind = document.createElement("div");
        kind.className = "kind";
        kind.textContent = drop.suspicious
            ? `dropped — ${drop.reason} — check this`
            : `dropped — ${drop.reason}`;
        const text = document.createElement("p");
        text.textContent = drop.text;
        div.append(kind, text);
        host.appendChild(div);
    });
}
