// @ts-check

function appendCell(row, value) {
  const cell = document.createElement("td");
  cell.textContent = value;
  row.appendChild(cell);
  return cell;
}

/** @type import("../../../../frontend/src/extension-api").ExtensionModule */
export default {
  init() {},
  onExtensionPageLoad(ctx) {
    const { api } = ctx;
    const filesTable = document.getElementById("bb-import-files");
    const accountSection = document.getElementById("bb-import-account-section");
    const accountSelect = /** @type {HTMLSelectElement | null} */ (document.getElementById("bb-import-account"));
    const accountContinue = /** @type {HTMLButtonElement | null} */ (
      document.getElementById("bb-import-account-continue")
    );
    const planSection = document.getElementById("bb-import-plan");
    const planBody = document.getElementById("bb-import-plan-body");
    const categories = document.getElementById("bb-import-categories");
    const currentFile = document.getElementById("bb-import-current-file");
    const currentAccount = document.getElementById("bb-import-current-account");
    const applyButton = /** @type {HTMLButtonElement | null} */ (document.getElementById("bb-import-apply"));
    const status = document.getElementById("bb-import-status");

    if (
      !filesTable || !accountSection || !accountSelect || !accountContinue || !planSection || !planBody ||
      !categories || !currentFile || !currentAccount || !applyButton || !status
    ) {
      return;
    }

    let sourceId = "";
    let sourceRow = null;
    let currentPlan = null;

    const showError = (error) => {
      status.textContent = error instanceof Error ? error.message : String(error);
    };

    const renderPlan = (plan) => {
      currentPlan = plan;
      accountSection.hidden = true;
      planSection.hidden = false;
      currentFile.textContent = plan.source_id;
      currentAccount.textContent = plan.account;
      planBody.replaceChildren();
      categories.replaceChildren();

      for (const category of plan.candidate_categories) {
        const option = document.createElement("option");
        option.value = category;
        categories.appendChild(option);
      }
      for (const transaction of plan.transactions) {
        const row = document.createElement("tr");
        row.dataset.rowId = transaction.row_id;
        row.dataset.originalAmount = transaction.amount;
        if (transaction.duplicate) {
          row.title = "An identical transaction already exists; this row is skipped by default.";
        }

        const skipCell = appendCell(row, "");
        const skip = document.createElement("input");
        skip.type = "checkbox";
        skip.className = "bb-import-skip";
        skip.checked = transaction.duplicate;
        skip.setAttribute("aria-label", `Skip ${transaction.payee}`);
        skipCell.appendChild(skip);
        appendCell(row, transaction.date);
        appendCell(row, transaction.payee);

        const amountCell = appendCell(row, "");
        const amount = document.createElement("input");
        amount.type = "number";
        amount.step = "0.01";
        amount.className = "bb-import-amount";
        amount.value = transaction.amount;
        amountCell.appendChild(amount);

        const categoryCell = appendCell(row, "");
        const category = document.createElement("input");
        category.type = "text";
        category.className = "bb-import-category";
        category.value = transaction.category;
        category.setAttribute("list", "bb-import-categories");
        categoryCell.appendChild(category);
        planBody.appendChild(row);
      }
      status.textContent = `${plan.transactions.length} transaction(s) ready for review.`;
    };

    const loadPlan = async (selectedAccount = "") => {
      status.textContent = "Reading statement…";
      try {
        const query = { source_id: sourceId };
        if (selectedAccount) {
          query.selected_account = selectedAccount;
        }
        const data = await api.get("plan", query);
        if (data.error) {
          showError(data.error);
          return;
        }
        if (data.status === "needs_account") {
          accountSelect.replaceChildren();
          for (const account of data.accounts) {
            const option = document.createElement("option");
            option.value = account;
            option.textContent = account;
            accountSelect.appendChild(option);
          }
          planSection.hidden = true;
          accountSection.hidden = false;
          status.textContent = `Choose the ${data.label} account for this statement.`;
          return;
        }
        renderPlan(data);
      } catch (error) {
        showError(error);
      }
    };

    filesTable.querySelectorAll(".bb-import-review").forEach((button) => {
      button.addEventListener("click", async (event) => {
        sourceRow = event.target?.closest("tr") ?? null;
        sourceId = sourceRow?.dataset.sourceId ?? "";
        currentPlan = null;
        accountSection.hidden = true;
        planSection.hidden = true;
        await loadPlan();
      });
    });

    accountContinue.addEventListener("click", async () => {
      await loadPlan(accountSelect.value);
    });

    applyButton.addEventListener("click", async () => {
      if (!currentPlan) {
        return;
      }
      const edits = Array.from(planBody.querySelectorAll("tr")).map((row) => {
        const amount = /** @type {HTMLInputElement | null} */ (row.querySelector(".bb-import-amount"));
        const category = /** @type {HTMLInputElement | null} */ (row.querySelector(".bb-import-category"));
        const skip = /** @type {HTMLInputElement | null} */ (row.querySelector(".bb-import-skip"));
        return {
          row_id: row.dataset.rowId,
          category: category?.value ?? "",
          new_amount: amount?.value === row.dataset.originalAmount ? null : amount?.value,
          deleted: skip?.checked === true,
        };
      });

      applyButton.disabled = true;
      status.textContent = "Validating and importing…";
      try {
        const result = await api.post("apply", {
          source_id: currentPlan.source_id,
          source_sha256: currentPlan.source_sha256,
          importer_id: currentPlan.importer_id,
          account: currentPlan.account,
          edits,
        });
        if (result.error) {
          showError(result.error);
          return;
        }
        status.textContent = result.message ?? result.status;
        if (result.status === "applied" || result.status === "already_applied") {
          sourceRow?.remove();
          planSection.hidden = true;
        }
      } catch (error) {
        showError(error);
      } finally {
        applyButton.disabled = false;
      }
    });
  },
};
