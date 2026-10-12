(() => {
  const createIdempotencyKey = () => {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };

  const dateElement = document.getElementById("current-date");
  const timeElement = document.getElementById("current-time");
  const startNewInspection = document.getElementById("start-new-inspection");
  startNewInspection?.addEventListener("click", () => {
    const status = document.getElementById("restore-conflict-status");
    const showStatus = (message) => {
      if (!status) return;
      status.textContent = message;
      status.hidden = false;
    };
    const storageKey = "office-terminal:inspection-draft";
    const target = {
      recoveryEpoch: startNewInspection.dataset.discardRestoreEpoch,
      idempotencyKey: startNewInspection.dataset.discardIdempotencyKey,
      revision: startNewInspection.dataset.discardDraftRevision,
    };
    const currentEpoch = startNewInspection.dataset.currentRestoreEpoch;
    const canIdentifyTarget = startNewInspection.dataset.discardTargetAvailable === "true";
    const readDraft = (raw) => {
      if (!raw) return null;
      try {
        return JSON.parse(raw);
      } catch (error) {
        return null;
      }
    };
    const matchesTarget = (raw) => {
      if (!raw || !canIdentifyTarget) return false;
      const draft = readDraft(raw);
      return draft?.version === 3 &&
        draft.recoveryEpoch === target.recoveryEpoch &&
        draft.idempotencyKey === target.idempotencyKey &&
        draft.revision === target.revision;
    };
    const belongsToCurrentEpoch = (raw) => readDraft(raw)?.recoveryEpoch === currentEpoch;
    let observedRaw;
    try {
      observedRaw = window.localStorage.getItem(storageKey);
    } catch (error) {
      showStatus("ブラウザの保存領域を確認できないため、下書きを破棄できません。");
      return;
    }

    if (observedRaw !== null) {
      if (belongsToCurrentEpoch(observedRaw)) {
        showStatus("現在の復元世代の下書きがあるため保持しました。点検画面から内容を確認してください。");
        return;
      }
      if (!matchesTarget(observedRaw) || target.recoveryEpoch === currentEpoch) {
        showStatus("別のタブで下書きが更新または置き換えられたため保持しました。点検画面から最新の内容を確認してください。");
        return;
      }
    }
    if (!window.confirm("古い下書きを破棄して、新しい点検を開始します。よろしいですか？")) return;

    try {
      const latestRaw = window.localStorage.getItem(storageKey);
      if (observedRaw === null) {
        if (latestRaw !== null) {
          showStatus("確認中に別のタブで下書きが作成されたため保持しました。点検画面から内容を確認してください。");
          return;
        }
      } else {
        if (latestRaw !== observedRaw || !matchesTarget(latestRaw) ||
            target.recoveryEpoch === currentEpoch) {
          showStatus("確認中に別のタブで下書きが更新または置き換えられたため保持しました。点検画面から最新の内容を確認してください。");
          return;
        }
        window.localStorage.removeItem(storageKey);
      }
      window.location.assign("/inspection");
    } catch (error) {
      showStatus("下書きを確認または破棄できませんでした。ブラウザの保存領域を確認してください。");
    }
  });

  if (dateElement && timeElement) {
    const dateFormatter = new Intl.DateTimeFormat("ja-JP", {
      timeZone: "Asia/Tokyo",
      year: "numeric",
      month: "long",
      day: "numeric",
      weekday: "short",
    });
    const timeFormatter = new Intl.DateTimeFormat("ja-JP", {
      timeZone: "Asia/Tokyo",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });

    const updateClock = () => {
      const now = new Date();
      dateElement.textContent = dateFormatter.format(now);
      timeElement.textContent = timeFormatter.format(now);
    };

    updateClock();
    window.setInterval(updateClock, 1000);
  }

  document.querySelectorAll(".inspection-item").forEach((item) => {
    const results = item.querySelectorAll('input[type="radio"][name^="result_"]');
    const reasonOptions = item.querySelector(".reason-options");
    const reasons = item.querySelectorAll('input[type="radio"][name^="reason_"]');
    if (!reasonOptions || results.length === 0) return;

    const updateReasonRequirement = () => {
      const selected = item.querySelector('input[type="radio"]:checked');
      const isRequired = Boolean(selected && selected.name.startsWith("result_") && selected.value !== "confirmed");
      reasonOptions.hidden = !isRequired;
      reasons.forEach((reason) => {
        reason.required = isRequired;
        if (!isRequired) reason.checked = false;
      });
    };

    results.forEach((result) => result.addEventListener("change", updateReasonRequirement));
    updateReasonRequirement();
  });

  const isHome = Boolean(document.querySelector(".home-screen"));
  const idleReturnDisabled = Boolean(document.querySelector('[data-idle-return="disabled"]'));
  const idleTimeoutMs = Number(window.OFFICE_TERMINAL_IDLE_TIMEOUT_MS ?? 60_000);
  let submissionPending = false;
  if (!isHome && !idleReturnDisabled && Number.isFinite(idleTimeoutMs) && idleTimeoutMs > 0) {
    let lastActivity = Date.now();
    let activityVersion = 0;
    let idleTimer;
    let saving = false;
    let draftStorageUnavailable = false;
    let draftConflict = false;

    const inspectionForm = document.querySelector(".inspection-form");
    const draftStatus = document.getElementById("draft-status");
    const showDraftStatus = (message, withReload = false) => {
      if (!draftStatus) return;
      draftStatus.textContent = message;
      draftStatus.hidden = false;
      if (withReload && !draftStatus.querySelector("button")) {
        const reload = document.createElement("button");
        reload.type = "button";
        reload.textContent = "再読み込み";
        reload.addEventListener("click", () => window.location.reload());
        draftStatus.append(" ", reload);
      }
    };

    const markStorageUnavailable = () => {
      if (draftStorageUnavailable) return;
      draftStorageUnavailable = true;
      showDraftStatus("ブラウザの保存領域を利用できないため、下書きを保持できません。点検入力中の自動復帰を停止しています。");
    };

    const checkIdleTimeout = (expectedVersion) => {
      if (saving || submissionPending || draftStorageUnavailable || expectedVersion !== activityVersion) return;
      const remaining = idleTimeoutMs - (Date.now() - lastActivity);
      if (remaining > 0) {
        idleTimer = window.setTimeout(() => checkIdleTimeout(expectedVersion), remaining);
        return;
      }
      window.location.assign("/");
    };

    const scheduleIdleReturn = () => {
      window.clearTimeout(idleTimer);
      const expectedVersion = activityVersion;
      idleTimer = window.setTimeout(() => {
        if (saving || submissionPending || draftStorageUnavailable || expectedVersion !== activityVersion) return;
        checkIdleTimeout(expectedVersion);
      }, idleTimeoutMs);
    };

    const recordActivity = () => {
      if (saving || submissionPending || draftStorageUnavailable) return;
      lastActivity = Date.now();
      activityVersion += 1;
      scheduleIdleReturn();
    };

    ["click", "keydown", "pointerdown", "wheel", "touchmove", "scroll"].forEach((eventName) => {
      document.addEventListener(eventName, recordActivity, { passive: true });
    });
    scheduleIdleReturn();

    if (inspectionForm) {
      const storageKey = "office-terminal:inspection-draft";
      const itemCodes = Array.from(inspectionForm.querySelectorAll(".inspection-item")).map((item) => {
        const input = item.querySelector('input[type="radio"]');
        return input ? input.name.slice("result_".length) : "";
      }).filter(Boolean);
      const getStorage = () => window.localStorage;
      let storage;
      let lastSeenRaw = null;
      let activeDraft = null;

      const setConflict = () => {
        if (draftConflict) return;
        draftConflict = true;
        showDraftStatus("別のタブで下書きが更新または破棄されました。再読み込みして最新の内容を確認してください。", true);
      };

      const readRaw = () => {
        try {
          return storage.getItem(storageKey);
        } catch (error) {
          markStorageUnavailable();
          return null;
        }
      };

      const isValidDraft = (draft) => {
        if (!draft || typeof draft !== "object" || draft.version !== 3) return false;
        if (draft.recoveryEpoch !== inspectionForm.dataset.restoreEpoch) return false;
        if (typeof draft.updatedAt !== "string" || !Number.isFinite(Date.parse(draft.updatedAt))) return false;
        if (typeof draft.startDate !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(draft.startDate)) return false;
        if (typeof draft.employeeId !== "string" || typeof draft.employeeName !== "string") return false;
        const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
        if (typeof draft.idempotencyKey !== "string" || !uuidPattern.test(draft.idempotencyKey)) return false;
        if (typeof draft.revision !== "string" || !uuidPattern.test(draft.revision)) return false;
        if (!draft.results || typeof draft.results !== "object" ||
            !draft.reasons || typeof draft.reasons !== "object") return false;
        return itemCodes.every((code) =>
          ["", "confirmed", "abnormal", "not_done"].includes(draft.results[code] ?? "") &&
          ((draft.reasons[code] ?? "") === "" ||
            Array.from(inspectionForm.querySelectorAll(`input[name="reason_${code}"]`))
              .some((input) => input.value === draft.reasons[code]))
        );
      };

      const hasDraftData = (draft) => Boolean(
        draft.employeeId ||
        itemCodes.some((code) => draft.results[code] || draft.reasons[code])
      );

      const hasDraftContentChanged = (draft) => (
        activeDraft &&
        (draft.employeeId !== activeDraft.employeeId ||
          itemCodes.some((code) =>
            draft.results[code] !== (activeDraft.results[code] ?? "") ||
            draft.reasons[code] !== (activeDraft.reasons[code] ?? "")
          ))
      );

      const updateRevisionField = (revision) => {
        const field = inspectionForm.elements.namedItem("draft_revision");
        if (field) field.value = revision;
      };

      const readFormDraft = (previous) => {
        const results = {};
        const reasons = {};
        itemCodes.forEach((code) => {
          results[code] = inspectionForm.querySelector(`input[name="result_${code}"]:checked`)?.value ?? "";
          reasons[code] = inspectionForm.querySelector(`input[name="reason_${code}"]:checked`)?.value ?? "";
        });
        const employeeId = inspectionForm.elements.namedItem("employee_id").value;
        const employeeOption = inspectionForm.elements.namedItem("employee_id").selectedOptions[0];
        const idempotencyKey = inspectionForm.elements.namedItem("idempotency_key").value;
        return {
          version: 3,
          recoveryEpoch: inspectionForm.dataset.restoreEpoch,
          updatedAt: new Date().toISOString(),
          revision: createIdempotencyKey(),
          startDate: previous?.startDate ?? inspectionForm.dataset.startDate,
          employeeId,
          employeeName: employeeOption?.dataset.displayName ?? "",
          results,
          reasons,
          idempotencyKey,
        };
      };

      const removeDraft = () => {
        if (draftStorageUnavailable) return false;
        const current = readRaw();
        if (draftStorageUnavailable) return false;
        if (draftConflict || current !== lastSeenRaw) {
          setConflict();
          return false;
        }
        try {
          storage.removeItem(storageKey);
          lastSeenRaw = null;
          activeDraft = null;
          updateRevisionField("");
          const discardButton = document.getElementById("discard-draft");
          if (discardButton) discardButton.hidden = true;
          return true;
        } catch (error) {
          markStorageUnavailable();
          return false;
        }
      };

      const persistDraft = () => {
        if (draftConflict || draftStorageUnavailable || saving) return;
        const current = readRaw();
        if (draftStorageUnavailable) return;
        if (current !== lastSeenRaw) {
          setConflict();
          return;
        }
        const draft = readFormDraft(activeDraft);
        if (hasDraftContentChanged(draft)) {
          draft.idempotencyKey = createIdempotencyKey();
          inspectionForm.elements.namedItem("idempotency_key").value = draft.idempotencyKey;
        }
        if (!hasDraftData(draft)) {
          if (lastSeenRaw !== null) removeDraft();
          return;
        }
        updateRevisionField(draft.revision);
        try {
          const serialized = JSON.stringify(draft);
          storage.setItem(storageKey, serialized);
          lastSeenRaw = serialized;
          activeDraft = draft;
          const discardButton = document.getElementById("discard-draft");
          if (discardButton) discardButton.hidden = false;
        } catch (error) {
          markStorageUnavailable();
        }
      };

      const clearForm = ({ preserveEmployee = false } = {}) => {
        const employeeId = preserveEmployee
          ? inspectionForm.elements.namedItem("employee_id").value
          : "";
        inspectionForm.querySelectorAll('input[type="radio"]').forEach((result) => {
          result.checked = false;
        });
        itemCodes.forEach((code) => {
          const item = inspectionForm.querySelector(`input[name="result_${code}"]`)?.closest(".inspection-item");
          item?.querySelectorAll(`input[name="reason_${code}"]`).forEach((reason) => {
            reason.checked = false;
            reason.required = false;
          });
          const reasonOptions = item?.querySelector(".reason-options");
          if (reasonOptions) reasonOptions.hidden = true;
        });
        inspectionForm.elements.namedItem("employee_id").value = employeeId;
      };

      const restoreDraft = (draft) => {
        inspectionForm.elements.namedItem("idempotency_key").value = draft.idempotencyKey;
        inspectionForm.elements.namedItem("employee_id").value = draft.employeeId;
        itemCodes.forEach((code) => {
          const checked = inspectionForm.querySelector(`input[name="result_${code}"][value="${draft.results[code] ?? ""}"]`);
          if (checked) checked.checked = true;
          const reason = inspectionForm.querySelector(
            `input[name="reason_${code}"][value="${draft.reasons[code] ?? ""}"]`
          );
          if (reason) reason.checked = true;
        });
        updateRevisionField(draft.revision);
        activeDraft = draft;
        const discardButton = document.getElementById("discard-draft");
        if (discardButton) discardButton.hidden = false;
        inspectionForm.querySelectorAll('.inspection-item input[name^="result_"]').forEach((result) => {
          result.dispatchEvent(new Event("change", { bubbles: true }));
        });
      };

      try {
        storage = getStorage();
        const initialRaw = storage.getItem(storageKey);
        lastSeenRaw = initialRaw;

        const success = new URLSearchParams(window.location.search);
        if (success.get("saved") && success.get("saved_key")) {
          let savedDraft = null;
          try {
            savedDraft = initialRaw ? JSON.parse(initialRaw) : null;
          } catch (error) {
            showDraftStatus("下書きの形式を読み取れません。点検を続けると新しい下書きに置き換わります。");
          }
          if (isValidDraft(savedDraft) &&
              savedDraft.idempotencyKey === success.get("saved_key") &&
              savedDraft.revision === success.get("saved_revision")) {
            storage.removeItem(storageKey);
            lastSeenRaw = null;
          }
        }

        const renderedWithErrors = inspectionForm.dataset.preserveRendered === "true";
        let draft = null;
        if (lastSeenRaw !== null) {
          try {
            draft = JSON.parse(lastSeenRaw);
          } catch (error) {
            showDraftStatus("下書きの形式を読み取れません。点検を続けると新しい下書きに置き換わります。");
          }
        }
        if (draft && draft.version !== 3) {
          if (!window.confirm("旧形式の下書きには自由入力の理由や備考が含まれるため、新しい形式へ引き継げません。内容を確認して旧形式の下書きを破棄し、新しい点検を開始しますか？")) {
            window.location.replace("/");
            return;
          }
          recordActivity();
          if (!removeDraft()) return;
          clearForm();
          inspectionForm.elements.namedItem("idempotency_key").value = createIdempotencyKey();
          showDraftStatus("旧形式の下書きを破棄しました。内容は引き継がれていません。点検を新しく開始できます。");
        } else if (draft && draft.recoveryEpoch !== inspectionForm.dataset.restoreEpoch) {
          if (!window.confirm("アプリが復元された可能性があります。復元前の下書きは安全のため引き継がず、破棄して新しく点検を始めますか？")) {
            window.location.replace("/");
            return;
          }
          recordActivity();
          if (!removeDraft()) return;
          clearForm();
          inspectionForm.elements.namedItem("idempotency_key").value = createIdempotencyKey();
          showDraftStatus("復元前の下書きを破棄しました。点検を新しく開始できます。");
        } else if (renderedWithErrors) {
          const rendered = readFormDraft(null);
          const revision = inspectionForm.elements.namedItem("draft_revision").value;
          const latestIsValid = isValidDraft(draft);
          if (revision && latestIsValid) {
            if (draft.revision !== revision) {
              setConflict();
            } else {
              activeDraft = draft;
              document.getElementById("discard-draft").hidden = false;
            }
          } else if ((revision && lastSeenRaw === null) || (!revision && latestIsValid)) {
            setConflict();
          } else if (hasDraftData(rendered)) {
            const serialized = JSON.stringify(rendered);
            storage.setItem(storageKey, serialized);
            lastSeenRaw = serialized;
            activeDraft = rendered;
            updateRevisionField(rendered.revision);
            document.getElementById("discard-draft").hidden = false;
          }
        } else if (draft && !isValidDraft(draft)) {
          showDraftStatus("下書きの形式が正しくありません。内容を保持しています。点検を再読み込みして確認してください。", true);
          setConflict();
        } else if (draft) {
          const employeeSelect = inspectionForm.elements.namedItem("employee_id");
          const option = Array.from(employeeSelect.options).find((candidate) => candidate.value === draft.employeeId);
          const dateNotice = draft.startDate === inspectionForm.dataset.startDate
            ? ""
            : `（開始日：${draft.startDate}）`;
          if (!draft.employeeId) {
            if (!window.confirm(`点検者未選択の下書きです。点検者を選んで再開しますか？${dateNotice}`)) {
              window.location.replace("/");
              return;
            }
            recordActivity();
            restoreDraft(draft);
            showDraftStatus("下書きを復元しました。点検者を選択してください。");
          } else if (!option) {
            if (!window.confirm("選択されていた点検者は有効ではありません。入力内容をリセットし、点検者を選び直します。続行しますか？")) {
              window.location.replace("/");
              return;
            }
            recordActivity();
            if (!removeDraft()) return;
            clearForm();
            inspectionForm.elements.namedItem("idempotency_key").value = createIdempotencyKey();
            showDraftStatus("点検者が無効のため、入力をリセットしました。点検者を選び直してください。");
          } else {
            const name = option.dataset.displayName || draft.employeeName || "点検者";
            if (!window.confirm(`${name}さんの点検を再開しますか？${dateNotice}`)) {
              window.location.replace("/");
              return;
            }
            recordActivity();
            restoreDraft(draft);
            showDraftStatus("下書きを復元しました。");
          }
        }
      } catch (error) {
        markStorageUnavailable();
      }

      const employeeSelect = inspectionForm.elements.namedItem("employee_id");
      const persistNonEmployeeInput = (event) => {
        if (event.target === employeeSelect) return;
        persistDraft();
      };
      inspectionForm.addEventListener("input", persistNonEmployeeInput);
      inspectionForm.addEventListener("change", persistNonEmployeeInput);
      employeeSelect.addEventListener("change", (event) => {
        if (!activeDraft?.employeeId || event.target.value === activeDraft.employeeId) {
          persistDraft();
          return;
        }
        if (window.confirm("点検者を変更すると、入力内容をリセットします。よろしいですか？")) {
          if (!removeDraft()) return;
          clearForm({ preserveEmployee: true });
          inspectionForm.elements.namedItem("idempotency_key").value = createIdempotencyKey();
          activeDraft = null;
          persistDraft();
        } else {
          event.target.value = activeDraft.employeeId;
        }
      });

      document.getElementById("discard-draft")?.addEventListener("click", () => {
        if (!window.confirm("下書きを破棄します。よろしいですか？")) return;
        if (removeDraft()) {
          clearForm();
          inspectionForm.elements.namedItem("idempotency_key").value = createIdempotencyKey();
          activeDraft = null;
          showDraftStatus("下書きを破棄しました。");
        }
      });

      inspectionForm.addEventListener("submit", (event) => {
        if (draftConflict) {
          event.preventDefault();
          showDraftStatus("別のタブで下書きが更新または破棄されました。再読み込みして最新の内容を確認してください。", true);
          return;
        }
        const current = readRaw();
        if (!draftStorageUnavailable && current !== lastSeenRaw) {
          event.preventDefault();
          setConflict();
          return;
        }
        saving = true;
        submissionPending = true;
        window.clearTimeout(idleTimer);
        inspectionForm.inert = true;
        inspectionForm.setAttribute("aria-busy", "true");
      });

      window.addEventListener("storage", (event) => {
        if (event.key === storageKey && event.newValue !== lastSeenRaw) setConflict();
      });

      const attendanceConsistency = document.querySelector(".attendance-consistency");
      const attendanceMessage = document.getElementById("attendance-consistency-message");
      const attendanceList = document.getElementById("attendance-consistency-list");
      const confirmedEveryoneLeft = inspectionForm.querySelector(
        'input[name="result_everyone_left"][value="confirmed"]'
      );
      if (attendanceConsistency && attendanceMessage && attendanceList && confirmedEveryoneLeft) {
        const statusUrl = "/api/inspection/attendance";
        const inspectorSelect = inspectionForm.elements.namedItem("employee_id");
        let selectedInspectorId = inspectorSelect.value;
        let attendanceState = attendanceList.childElementCount > 0
          ? "conflict"
          : (inspectorSelect.value ? "loading" : "unselected");
        let conflicts = Array.from(attendanceList.children).map((item) => {
          const match = item.textContent.match(/^(.*)（(在室|離席中)）$/);
          return match ? { display_name: match[1], status: match[2] } : null;
        }).filter(Boolean);
        let requestController = null;
        let requestTimeout = null;
        let requestVersion = 0;

        const clearConfirmedResult = () => {
          if (!confirmedEveryoneLeft.checked) return;
          confirmedEveryoneLeft.checked = false;
          const reasonOptions = confirmedEveryoneLeft.closest(".inspection-item")
            ?.querySelector(".reason-options");
          if (reasonOptions) {
            reasonOptions.hidden = true;
            reasonOptions.querySelectorAll('input[type="radio"]').forEach((input) => {
              input.checked = false;
              input.required = false;
            });
          }
          persistDraft();
        };

        const renderAttendanceState = () => {
          confirmedEveryoneLeft.disabled = true;
          attendanceList.replaceChildren();
          if (!inspectorSelect.value) {
            attendanceState = "unselected";
            attendanceMessage.textContent = "点検者を選択すると在席状態を確認します。";
            attendanceList.hidden = true;
            return;
          }
          if (attendanceState === "loading") {
            attendanceMessage.textContent = "登録上の在席状態を確認しています。";
            attendanceList.hidden = true;
            return;
          }
          if (attendanceState === "unknown") {
            attendanceMessage.textContent =
              "登録上の在席状態を確認できません。通信が回復するまで「確認済み」は選択できません。";
            attendanceList.hidden = true;
            return;
          }
          if (attendanceState === "unavailable") {
            attendanceMessage.textContent =
              "登録上の在席状態を確認できません。通信が回復するまで「確認済み」は選択できません。";
            attendanceList.hidden = true;
            return;
          }
          if (attendanceState === "conflict") {
            attendanceMessage.textContent =
              "点検者以外に在室・離席中の社員が登録されています。";
            conflicts.forEach((person) => {
              const entry = document.createElement("li");
              entry.textContent = `${person.display_name}（${person.status}）`;
              attendanceList.append(entry);
            });
            attendanceList.hidden = false;
            return;
          }
          attendanceMessage.textContent =
            "登録上、点検者以外に在室・離席中の社員はいません。";
          attendanceList.hidden = true;
          confirmedEveryoneLeft.disabled = false;
        };

        const refreshAttendanceState = async (force = false) => {
          const employeeId = inspectorSelect.value;
          if (employeeId !== selectedInspectorId) {
            selectedInspectorId = employeeId;
            attendanceState = employeeId ? "loading" : "unselected";
            conflicts = [];
            renderAttendanceState();
            if (employeeId) clearConfirmedResult();
          }
          if (!employeeId) {
            renderAttendanceState();
            return;
          }
          if (requestController && !force) return;
          if (requestController) {
            requestController.abort();
            window.clearTimeout(requestTimeout);
            requestController = null;
          }

          const controller = new AbortController();
          requestController = controller;
          const version = ++requestVersion;
          requestTimeout = window.setTimeout(() => controller.abort(), 5000);
          try {
            const response = await window.fetch(
              `${statusUrl}?employee_id=${encodeURIComponent(employeeId)}`,
              { cache: "no-store", signal: controller.signal }
            );
            if (!response.ok) throw new Error("在席状態の取得に失敗しました");
            const payload = await response.json();
            if (!payload || !Array.isArray(payload.conflicts) ||
                !payload.conflicts.every((person) =>
                  person && typeof person.display_name === "string" &&
                  ["在室", "離席中"].includes(person.status)
                )) {
              throw new Error("在席状態の応答形式が正しくありません");
            }
            if (version !== requestVersion || inspectorSelect.value !== employeeId) return;
            conflicts = payload.conflicts;
            attendanceState = conflicts.length ? "conflict" : "clear";
            if (conflicts.length) clearConfirmedResult();
            renderAttendanceState();
          } catch (error) {
            if (version !== requestVersion || inspectorSelect.value !== employeeId) return;
            attendanceState = "unavailable";
            conflicts = [];
            clearConfirmedResult();
            renderAttendanceState();
          } finally {
            if (version === requestVersion) {
              window.clearTimeout(requestTimeout);
              requestController = null;
            }
          }
        };

        inspectorSelect.addEventListener("change", () => refreshAttendanceState(true));
        renderAttendanceState();
        if (attendanceState === "conflict") clearConfirmedResult();
        refreshAttendanceState(true);
        window.setInterval(() => refreshAttendanceState(), 10_000);
      }
    }
  }

  const homeStatus = document.getElementById("home-draft-status");
  const homeDraftActions = document.getElementById("home-draft-actions");
  const resumeDraft = document.getElementById("resume-draft");
  const discardHomeDraft = document.getElementById("discard-home-draft");
  if (isHome && homeStatus && resumeDraft) {
    const storageKey = "office-terminal:inspection-draft";
    try {
      const storage = window.localStorage;
      const params = new URLSearchParams(window.location.search);
      const raw = storage.getItem(storageKey);
      if (params.get("saved") && params.get("saved_key") && raw) {
        let draft = null;
        try {
          draft = JSON.parse(raw);
        } catch (error) {
          draft = null;
        }
        if (draft && draft.version === 3 &&
            draft.idempotencyKey === params.get("saved_key") &&
            draft.revision === params.get("saved_revision") &&
            draft.recoveryEpoch === homeDraftActions.dataset.restoreEpoch) {
          storage.removeItem(storageKey);
        }
      }
      const current = storage.getItem(storageKey);
      if (current) {
        let draft;
        try {
          draft = JSON.parse(current);
        } catch (error) {
          throw new Error("下書きの形式が正しくありません。");
        }
        if (!draft || typeof draft.employeeName !== "string" ||
            typeof draft.employeeId !== "string" || typeof draft.startDate !== "string") {
          throw new Error("下書きの形式が正しくありません。");
        }
        if (draft.version !== 3 || draft.recoveryEpoch !== homeDraftActions.dataset.restoreEpoch) {
          homeDraftActions.hidden = false;
          homeStatus.textContent = draft.version !== 3
            ? "旧形式の下書きには自由入力が含まれるため再開できません。内容を確認し、破棄して新しい点検を開始してください。"
            : "復元前の下書きがあります。点検画面で確認し、安全のため破棄してください。";
          homeStatus.hidden = false;
          if (discardHomeDraft) discardHomeDraft.hidden = false;
          discardHomeDraft?.addEventListener("click", () => {
            const confirmation = draft.version !== 3
              ? "旧形式の自由入力を新しい選択肢へ引き継ぐことはできません。旧形式の下書きを破棄しますか？"
              : "復元前の下書きを破棄します。よろしいですか？";
            if (!window.confirm(confirmation)) return;
            try {
              if (storage.getItem(storageKey) !== current) {
                homeStatus.textContent = "別のタブで下書きが更新されました。再読み込みして最新の内容を確認してください。";
                return;
              }
              storage.removeItem(storageKey);
              homeStatus.textContent = "復元前の下書きを破棄しました。";
              discardHomeDraft.hidden = true;
              homeDraftActions.hidden = true;
            } catch (error) {
              homeStatus.textContent = "下書きを破棄できませんでした。ブラウザの保存領域を確認してください。";
            }
          });
          return;
        }
        homeDraftActions.hidden = false;
        resumeDraft.hidden = false;
        const inspector = draft.employeeName || "点検者未選択";
        resumeDraft.textContent = `${inspector}さんの点検を再開`;
        homeStatus.textContent = `保存された下書きがあります（開始日：${draft.startDate}）。`;
        homeStatus.hidden = false;
        if (discardHomeDraft) discardHomeDraft.hidden = false;
        discardHomeDraft?.addEventListener("click", () => {
          if (!window.confirm("下書きを破棄します。よろしいですか？")) return;
          try {
            if (storage.getItem(storageKey) !== current) {
              homeStatus.textContent = "別のタブで下書きが更新されました。再読み込みして最新の内容を確認してください。";
              return;
            }
            storage.removeItem(storageKey);
            homeStatus.textContent = "下書きを破棄しました。";
            resumeDraft.hidden = true;
            discardHomeDraft.hidden = true;
          } catch (error) {
            homeStatus.textContent = "下書きを破棄できませんでした。ブラウザの保存領域を確認してください。";
          }
        });
      }
    } catch (error) {
      homeStatus.textContent = error instanceof Error && error.message.includes("形式")
        ? error.message
        : "ブラウザの保存領域を利用できないため、下書きを確認できません。";
      homeStatus.hidden = false;
      homeDraftActions.hidden = false;
    }
  }

  const attendanceForm = document.querySelector(".attendance-form");
  if (attendanceForm) {
    const employeeSelect = attendanceForm.querySelector("#attendance-employee");
    const statusLabel = attendanceForm.querySelector("#attendance-status");
    const operationButtons = attendanceForm.querySelectorAll("[data-allowed-from]");
    const updateAttendanceControls = () => {
      const selected = employeeSelect.selectedOptions[0];
      const currentStatus = selected?.dataset.status ?? "";
      statusLabel.textContent = currentStatus || "未選択";
      operationButtons.forEach((button) => {
        const allowedStatuses = button.dataset.allowedFrom.split(",");
        button.hidden = !currentStatus || !allowedStatuses.includes(currentStatus);
      });
    };
    employeeSelect.addEventListener("change", updateAttendanceControls);
    attendanceForm.addEventListener("submit", (event) => {
      if (attendanceForm.dataset.submitting === "true") {
        event.preventDefault();
        return;
      }
      attendanceForm.dataset.submitting = "true";
      submissionPending = true;
      const submitter = event.submitter;
      operationButtons.forEach((button) => {
        if (button !== submitter) button.disabled = true;
      });
      if (submitter) submitter.textContent = "処理中…";
    });
    updateAttendanceControls();
  }

  const attendeeList = document.getElementById("attendee-list");
  const attendeeRefreshStatus = document.getElementById("attendee-refresh-status");
  if (attendeeList && attendeeRefreshStatus) {
    const presentCount = document.getElementById("attendee-present-count");
    const awayCount = document.getElementById("attendee-away-count");
    const noAttendees = document.getElementById("no-attendees");
    let refreshPending = false;

    const renderAttendees = (data) => {
      if (!Array.isArray(data.people) ||
          !Number.isInteger(data.present_count) ||
          !Number.isInteger(data.away_count)) {
        throw new Error("出社者データの形式が正しくありません。");
      }
      const fragment = document.createDocumentFragment();
      data.people.forEach((person) => {
        if (typeof person.employee_id !== "string" ||
            typeof person.name !== "string" ||
            !["在室", "離席中"].includes(person.status)) {
          throw new Error("出社者データの形式が正しくありません。");
        }
        const row = document.createElement("li");
        row.className = "attendee-item";
        row.dataset.employeeId = person.employee_id;
        const name = document.createElement("span");
        name.className = "attendee-name";
        name.textContent = person.name;
        const status = document.createElement("span");
        status.className = `attendee-status${person.status === "離席中" ? " away" : ""}`;
        status.textContent = person.status;
        row.append(name, status);
        fragment.append(row);
      });
      attendeeList.replaceChildren(fragment);
      presentCount.textContent = String(data.present_count);
      awayCount.textContent = String(data.away_count);
      noAttendees.hidden = data.people.length !== 0;
    };

    const refreshAttendees = async () => {
      if (refreshPending) return;
      refreshPending = true;
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 5000);
      try {
        const response = await window.fetch("/api/attendees", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        renderAttendees(data);
        attendeeRefreshStatus.textContent = "";
        attendeeRefreshStatus.hidden = true;
      } catch (error) {
        attendeeRefreshStatus.textContent = "出社者の最新状態を取得できていません。表示中の一覧を保持しています。";
        attendeeRefreshStatus.hidden = false;
      } finally {
        window.clearTimeout(timeout);
        refreshPending = false;
        window.setTimeout(refreshAttendees, 10_000);
      }
    };

    window.setTimeout(refreshAttendees, 10_000);
  }
})();

(() => {
  const calendarGrid = document.getElementById("calendar-grid");
  if (!calendarGrid) return;

  const refreshInterval = 10_000;
  const refreshAttendanceDates = async () => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const params = new URLSearchParams({
        year: calendarGrid.dataset.calendarYear,
        month: calendarGrid.dataset.calendarMonth,
      });
      const response = await window.fetch(`/api/calendar-arrivals?${params}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!data || !Array.isArray(data.dates) ||
          data.dates.some((day) => typeof day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(day))) {
        throw new Error("出社日データの形式が正しくありません。");
      }

      const attendanceDates = new Set(data.dates);
      calendarGrid.querySelectorAll(".day-cell").forEach((cell) => {
        const hasAttendance = attendanceDates.has(cell.dataset.date);
        let marks = cell.querySelector(".day-marks");
        let attendanceMark = cell.querySelector(".attendance-mark");

        if (hasAttendance && !attendanceMark) {
          if (!marks) {
            marks = document.createElement("span");
            marks.className = "day-marks";
            marks.setAttribute("aria-hidden", "true");
            cell.append(marks);
          }
          attendanceMark = document.createElement("span");
          attendanceMark.className = "attendance-mark";
          attendanceMark.textContent = "●";
          marks.append(attendanceMark);
        } else if (!hasAttendance && attendanceMark) {
          attendanceMark.remove();
          if (marks.childElementCount === 0) marks.remove();
        }

        const label = [
          `${cell.dataset.dateLabel}、${cell.dataset.statusName}`,
          ...(hasAttendance ? ["出社記録あり"] : []),
          ...(cell.dataset.isToday === "true" ? ["今日"] : []),
        ];
        cell.setAttribute("aria-label", label.join("、"));
      });
    } catch (error) {
      console.warn("出社日の更新に失敗しました。表示中のマークを保持します。", error);
    } finally {
      window.clearTimeout(timeout);
      window.setTimeout(refreshAttendanceDates, refreshInterval);
    }
  };

  window.setTimeout(refreshAttendanceDates, refreshInterval);
})();

(() => {
  const card = document.getElementById("weather-card");
  if (!card) return;

  const CLIENT_ERROR_TEXT = "天気情報を更新できていません";
  const isText = (value) => typeof value === "string";
  const isValidView = (view) => {
    if (!view || typeof view !== "object" || Array.isArray(view)) return false;
    if (view.status === "ok") {
      return ["region", "icon", "headline", "temperature", "pop_text", "issued"].every((key) => isText(view[key]))
        && (view.temperature_note === undefined || isText(view.temperature_note))
        && (view.notice === undefined || isText(view.notice));
    }
    if (!["loading", "unavailable", "config_error", "stale"].includes(view.status)
        || !isText(view.region)
        || !isText(view.message)) return false;
    return view.status !== "stale" || isText(view.issued);
  };

  const line = (className, ...children) => {
    const element = document.createElement("div");
    element.className = `weather-line ${className}`;
    element.append(...children);
    return element;
  };
  const span = (className, text) => {
    const element = document.createElement("span");
    if (className) element.className = className;
    element.textContent = text;
    return element;
  };

  // 再描画しても同じ要素を再利用し、失敗案内が増えないようにする。
  const clientNotice = line("weather-client-error", CLIENT_ERROR_TEXT);
  clientNotice.setAttribute("role", "status");
  clientNotice.hidden = true;
  card.append(clientNotice);

  const setClientError = (failed) => {
    clientNotice.hidden = !failed;
    if (failed) card.dataset.clientError = "1";
    else delete card.dataset.clientError;
  };

  const renderWeather = (view) => {
    const children = [];
    if (view.status === "ok") {
      children.push(line("weather-main", span("weather-place", view.region), span("weather-icon", view.icon), span("weather-headline", view.headline)));
      children.push(line("weather-values", span("weather-temperature", view.temperature), span("weather-pop", view.pop_text)));
      children.push(line("weather-meta", span("", view.issued), ...(view.temperature_note ? [document.createTextNode(" "), span("", view.temperature_note)] : [])));
      if (view.notice) {
        const notice = line("weather-notice", view.notice);
        notice.setAttribute("role", "status");
        children.push(notice);
      }
    } else {
      children.push(line("weather-main", span("weather-place", view.region || "")));
      const message = line("weather-message", view.message);
      message.setAttribute("role", "status");
      children.push(message);
      if (view.issued) children.push(line("weather-meta", span("", view.issued)));
    }
    card.dataset.status = view.status;
    card.replaceChildren(...children, clientNotice);
  };

  let pending = false;
  let failed = false;
  const refreshWeather = async () => {
    if (pending) return;
    pending = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await window.fetch("/api/weather", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const view = await response.json();
      if (!isValidView(view)) throw new Error("invalid weather response");
      renderWeather(view);
      failed = false;
      setClientError(false);
    } catch (error) {
      // 表示済みの天気は保持し、案内だけを出す。
      failed = true;
      setClientError(true);
    } finally {
      window.clearTimeout(timeout);
      pending = false;
    }
  };

  const nextDelay = () => (failed || card.dataset.status === "loading" ? 5_000 : 60_000);
  const schedule = async () => {
    await refreshWeather();
    window.setTimeout(schedule, nextDelay());
  };
  window.setTimeout(schedule, nextDelay());
})();
