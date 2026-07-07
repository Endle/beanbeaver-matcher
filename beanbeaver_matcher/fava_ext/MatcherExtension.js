// @ts-check

/** @type import("../../../../frontend/src/extension-api").ExtensionModule */
export default {
  init() {},
  onExtensionPageLoad(ctx) {
    const { api } = ctx;
    const table = document.getElementById("bb-matcher-receipts");
    const candidatesSection = document.getElementById("bb-matcher-candidates");
    const candidatesBody = document.getElementById("bb-matcher-candidates-body");
    const currentReceiptLabel = document.getElementById("bb-matcher-current-receipt");
    const warningLabel = document.getElementById("bb-matcher-warning");
    const statusLabel = document.getElementById("bb-matcher-status");

    if (!table || !candidatesSection || !candidatesBody || !currentReceiptLabel || !warningLabel || !statusLabel) {
      return;
    }

    table.querySelectorAll(".bb-matcher-select").forEach((button) => {
      button.addEventListener("click", async (event) => {
        const row = /** @type {HTMLElement | null} */ (event.target)?.closest("tr");
        const stagePath = row?.dataset.stagePath;
        if (!row || !stagePath) {
          return;
        }

        currentReceiptLabel.textContent = stagePath;
        candidatesBody.innerHTML = "<tr><td colspan=\"6\">Loading…</td></tr>";
        candidatesSection.hidden = false;
        warningLabel.textContent = "";
        statusLabel.textContent = "";

        const data = await api.get("candidates", { stage_path: stagePath });
        if (data.error) {
          candidatesBody.innerHTML = `<tr><td colspan="6">${data.error}</td></tr>`;
          return;
        }

        warningLabel.textContent = data.warning ?? "";
        if (!data.candidates.length) {
          candidatesBody.innerHTML = "<tr><td colspan=\"6\">No candidates found.</td></tr>";
          return;
        }

        candidatesBody.innerHTML = "";
        for (const candidate of data.candidates) {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${Math.round(candidate.confidence * 100)}%</td>
            <td>${candidate.date}</td>
            <td>${candidate.payee ?? ""}</td>
            <td>${candidate.amount ?? ""}</td>
            <td>${candidate.details}</td>
            <td><button type="button">Apply</button></td>
          `;
          const applyButton = tr.querySelector("button");
          applyButton?.addEventListener("click", async () => {
            statusLabel.textContent = "Applying…";
            const result = await api.post("apply", {
              stage_path: stagePath,
              file_path: candidate.file_path,
              line_number: candidate.line_number,
            });
            statusLabel.textContent = result.message ?? result.status;
            if (result.status === "applied" || result.status === "already_applied") {
              row.remove();
              candidatesSection.hidden = true;
            }
          });
          candidatesBody.appendChild(tr);
        }
      });
    });
  },
};
