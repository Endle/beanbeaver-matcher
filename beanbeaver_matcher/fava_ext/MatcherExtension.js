// @ts-check

function appendInputCell(row, className, type, value) {
  const cell = document.createElement("td");
  const input = document.createElement("input");
  input.className = className;
  input.type = type;
  input.value = value ?? "";
  if (type === "number") {
    input.step = className.includes("quantity") ? "1" : "0.01";
  }
  cell.appendChild(input);
  row.appendChild(cell);
  return input;
}

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
    const editor = document.getElementById("bb-matcher-editor");
    const editorPath = document.getElementById("bb-matcher-editor-path");
    const merchantInput = /** @type {HTMLInputElement | null} */ (document.getElementById("bb-matcher-edit-merchant"));
    const dateInput = /** @type {HTMLInputElement | null} */ (document.getElementById("bb-matcher-edit-date"));
    const subtotalInput = /** @type {HTMLInputElement | null} */ (document.getElementById("bb-matcher-edit-subtotal"));
    const taxInput = /** @type {HTMLInputElement | null} */ (document.getElementById("bb-matcher-edit-tax"));
    const totalInput = /** @type {HTMLInputElement | null} */ (document.getElementById("bb-matcher-edit-total"));
    const itemsBody = document.getElementById("bb-matcher-edit-items");
    const tendersBody = document.getElementById("bb-matcher-edit-tenders");
    const accountsList = document.getElementById("bb-matcher-accounts");
    const addItemButton = document.getElementById("bb-matcher-add-item");
    const addTenderButton = /** @type {HTMLButtonElement | null} */ (document.getElementById("bb-matcher-add-tender"));
    const saveButton = /** @type {HTMLButtonElement | null} */ (document.getElementById("bb-matcher-save-receipt"));
    const cancelButton = /** @type {HTMLButtonElement | null} */ (document.getElementById("bb-matcher-cancel-edit"));

    if (
      !table || !candidatesSection || !candidatesBody || !currentReceiptLabel || !warningLabel || !statusLabel ||
      !editor || !editorPath || !merchantInput || !dateInput || !subtotalInput || !taxInput || !totalInput ||
      !itemsBody || !tendersBody || !accountsList || !addItemButton || !addTenderButton || !saveButton || !cancelButton
    ) {
      return;
    }

    let editData = null;
    let editRow = null;

    const showError = (error) => {
      statusLabel.textContent = error instanceof Error ? error.message : String(error);
    };

    const appendRemoveCell = (row, label) => {
      const cell = document.createElement("td");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "muted";
      button.textContent = label;
      button.addEventListener("click", () => row.remove());
      cell.appendChild(button);
      row.appendChild(cell);
    };

    const appendItemRow = (item) => {
      const row = document.createElement("tr");
      if (item.source_index != null) row.dataset.sourceIndex = String(item.source_index);
      const description = appendInputCell(row, "bb-matcher-item-description", "text", item.description);
      appendInputCell(row, "bb-matcher-item-price", "number", item.price);
      appendInputCell(row, "bb-matcher-item-quantity", "number", String(item.quantity));
      const category = appendInputCell(row, "bb-matcher-item-category", "text", item.category);
      category.setAttribute("list", "bb-matcher-accounts");
      appendRemoveCell(row, "Remove item");
      itemsBody.appendChild(row);
      return description;
    };

    const appendTenderRow = (tender) => {
      const row = document.createElement("tr");
      const kindCell = document.createElement("td");
      const kind = document.createElement("select");
      kind.className = "bb-matcher-tender-kind";
      for (const value of ["card", "gift_card", "cash", "store_credit"]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value.replace("_", " ");
        option.selected = value === tender.kind;
        kind.appendChild(option);
      }
      kindCell.appendChild(kind);
      row.appendChild(kindCell);
      appendInputCell(row, "bb-matcher-tender-amount", "number", tender.amount);
      const account = appendInputCell(row, "bb-matcher-tender-account", "text", tender.account);
      account.setAttribute("list", "bb-matcher-accounts");
      appendInputCell(row, "bb-matcher-tender-label", "text", tender.raw_label);
      appendRemoveCell(row, "Remove tender");
      tendersBody.appendChild(row);
      return kind;
    };

    const renderEditor = (data, row) => {
      editData = data;
      editRow = row;
      editorPath.textContent = data.stage_path;
      document.getElementById("bb-matcher-edit-raw-text").textContent = data.raw_text || "No OCR text available.";
      const warnings = document.getElementById("bb-matcher-edit-warnings");
      warnings.replaceChildren();
      for (const message of data.warnings || []) {
        const warning = document.createElement("li");
        warning.textContent = message;
        warnings.appendChild(warning);
      }
      merchantInput.value = data.merchant;
      dateInput.value = data.date;
      subtotalInput.value = data.subtotal;
      taxInput.value = data.tax;
      totalInput.value = data.total;
      itemsBody.replaceChildren();
      tendersBody.replaceChildren();
      accountsList.replaceChildren();

      for (const account of data.accounts) {
        const option = document.createElement("option");
        option.value = account;
        accountsList.appendChild(option);
      }
      for (const item of data.items) {
        appendItemRow(item);
      }
      const tenders = data.tenders.length
        ? data.tenders
        : [{ kind: "card", amount: data.total, account: "", raw_label: "Card" }];
      for (const tender of tenders) {
        appendTenderRow(tender);
      }
      candidatesSection.hidden = true;
      editor.hidden = false;
      editor.scrollIntoView({ behavior: "smooth", block: "start" });
      statusLabel.textContent = "Edit the receipt and save to recalculate matches.";
    };

    const loadCandidates = async (row, stagePath) => {
      currentReceiptLabel.textContent = stagePath;
      candidatesBody.innerHTML = "<tr><td colspan=\"6\">Loading…</td></tr>";
      candidatesSection.hidden = false;
      candidatesSection.scrollIntoView({ behavior: "smooth", block: "start" });
      editor.hidden = true;
      warningLabel.textContent = "";
      statusLabel.textContent = "";

      try {
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
            try {
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
            } catch (error) {
              showError(error);
            }
          });
          candidatesBody.appendChild(tr);
        }
      } catch (error) {
        showError(error);
      }
    };

    table.querySelectorAll(".bb-matcher-select").forEach((button) => {
      button.addEventListener("click", async (event) => {
        const row = /** @type {HTMLElement | null} */ (event.target)?.closest("tr");
        const stagePath = row?.dataset.stagePath;
        if (row && stagePath) {
          await loadCandidates(row, stagePath);
        }
      });
    });

    table.querySelectorAll(".bb-matcher-edit").forEach((button) => {
      button.addEventListener("click", async (event) => {
        const row = /** @type {HTMLElement | null} */ (event.target)?.closest("tr");
        const stagePath = row?.dataset.stagePath;
        if (!row || !stagePath) {
          return;
        }
        statusLabel.textContent = "Loading receipt…";
        try {
          const data = await api.get("receipt", { stage_path: stagePath });
          if (data.error) {
            showError(data.error);
            return;
          }
          renderEditor(data, row);
        } catch (error) {
          showError(error);
        }
      });
    });

    table.querySelectorAll(".bb-matcher-delete-duplicate").forEach((element) => {
      const button = /** @type {HTMLButtonElement} */ (element);
      button.addEventListener("click", async () => {
        const row = button.closest("tr");
        if (!row || !window.confirm("Delete this pending receipt and its files? They will be moved to receipt trash and can be restored. The matched ledger entry will stay unchanged.")) {
          return;
        }
        button.disabled = true;
        try {
          const result = await api.post("delete-duplicate", {
            stage_path: row.dataset.stagePath,
            source_sha256: row.dataset.sourceSha256,
          });
          if (result.error) {
            showError(result.error);
            return;
          }
          row.remove();
          editor.hidden = true;
          candidatesSection.hidden = true;
          editData = null;
          editRow = null;
          statusLabel.textContent = result.message;
        } catch (error) {
          showError(error);
        } finally {
          button.disabled = false;
        }
      });
    });

    addItemButton.addEventListener("click", () => {
      appendItemRow({ description: "", price: "", quantity: 1, category: "" }).focus();
    });

    addTenderButton.addEventListener("click", () => {
      const giftCardAccount = editData?.accounts?.includes("Assets:PrepaidCard:GiftCard")
        ? "Assets:PrepaidCard:GiftCard"
        : "";
      appendTenderRow({ kind: "gift_card", amount: "", account: giftCardAccount, raw_label: "" }).focus();
    });

    cancelButton.addEventListener("click", () => {
      editor.hidden = true;
      editData = null;
      editRow = null;
      statusLabel.textContent = "";
    });

    saveButton.addEventListener("click", async () => {
      if (!editData || !editRow) {
        return;
      }
      const items = Array.from(itemsBody.querySelectorAll("tr")).map((row) => ({
        source_index: row.dataset.sourceIndex == null ? null : Number(row.dataset.sourceIndex),
        description: /** @type {HTMLInputElement} */ (row.querySelector(".bb-matcher-item-description")).value,
        price: /** @type {HTMLInputElement} */ (row.querySelector(".bb-matcher-item-price")).value,
        quantity: /** @type {HTMLInputElement} */ (row.querySelector(".bb-matcher-item-quantity")).value,
        category: /** @type {HTMLInputElement} */ (row.querySelector(".bb-matcher-item-category")).value,
      }));
      const tenders = Array.from(tendersBody.querySelectorAll("tr")).map((row) => ({
        kind: /** @type {HTMLSelectElement} */ (row.querySelector(".bb-matcher-tender-kind")).value,
        amount: /** @type {HTMLInputElement} */ (row.querySelector(".bb-matcher-tender-amount")).value,
        account: /** @type {HTMLInputElement} */ (row.querySelector(".bb-matcher-tender-account")).value,
        raw_label: /** @type {HTMLInputElement} */ (row.querySelector(".bb-matcher-tender-label")).value,
      }));

      saveButton.disabled = true;
      statusLabel.textContent = "Saving receipt…";
      try {
        const result = await api.post("edit-receipt", {
          stage_path: editData.stage_path,
          source_sha256: editData.source_sha256,
          merchant: merchantInput.value,
          date: dateInput.value,
          subtotal: subtotalInput.value,
          tax: taxInput.value,
          total: totalInput.value,
          items,
          tenders,
        });
        if (result.error) {
          showError(result.error);
          return;
        }
        editRow.dataset.stagePath = result.stage_path;
        editRow.dataset.sourceSha256 = result.source_sha256;
        editRow.querySelector(".bb-matcher-select").hidden = false;
        const cells = editRow.querySelectorAll("td");
        cells[0].textContent = result.date || "UNKNOWN";
        cells[1].textContent = result.merchant;
        cells[2].textContent = `$${result.total}`;
        editor.hidden = true;
        statusLabel.textContent = "Receipt saved. Recalculating matches…";
        await loadCandidates(editRow, result.stage_path);
      } catch (error) {
        showError(error);
      } finally {
        saveButton.disabled = false;
      }
    });
  },
};
