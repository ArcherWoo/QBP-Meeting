(function () {
  function parseJson(response) {
    if (!response.ok) {
      return response.json().then(function (data) {
        throw new Error(data.error || "请求失败");
      });
    }
    return response.json();
  }

  function roundSelectNear(versionSelect) {
    var form = versionSelect.closest("form") || document;
    return form.querySelector("[data-plan-round-select]");
  }

  function refreshRounds(versionSelect) {
    var roundSelect = roundSelectNear(versionSelect);
    if (!roundSelect || !versionSelect.value) return;
    return fetch("/plan-versions/" + encodeURIComponent(versionSelect.value) + "/rounds")
      .then(parseJson)
      .then(function (data) {
        roundSelect.innerHTML = "";
        if (roundSelect.name === "plan_round_id" && roundSelect.closest(".meeting-filter-grid")) {
          var empty = document.createElement("option");
          empty.value = "";
          empty.textContent = "全部 Round";
          roundSelect.appendChild(empty);
        }
        data.rounds.forEach(function (round) {
          var option = document.createElement("option");
          option.value = round.id;
          option.textContent = round.name;
          roundSelect.appendChild(option);
        });
        if (data.rounds.length) {
          roundSelect.value = data.rounds[0].id;
        }
      });
  }

  function createPlanVersion(versionSelect) {
    var name = window.prompt("Plan Version");
    if (!name) return;
    fetch("/plan-versions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name })
    })
      .then(parseJson)
      .then(function (version) {
        var option = document.createElement("option");
        option.value = version.id;
        option.textContent = version.name;
        option.selected = true;
        versionSelect.appendChild(option);
        refreshRounds(versionSelect);
      })
      .catch(function (error) { window.alert(error.message); });
  }

  function createRound(versionSelect) {
    if (!versionSelect.value) return;
    fetch("/plan-versions/" + encodeURIComponent(versionSelect.value) + "/rounds", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    })
      .then(parseJson)
      .then(function (round) {
        var roundSelect = roundSelectNear(versionSelect);
        if (!roundSelect) return;
        var option = document.createElement("option");
        option.value = round.id;
        option.textContent = round.name;
        option.selected = true;
        roundSelect.appendChild(option);
      })
      .catch(function (error) { window.alert(error.message); });
  }

  function deletePlanVersion(versionSelect) {
    if (!versionSelect.value) return;
    var selected = versionSelect.options[versionSelect.selectedIndex];
    var name = selected ? selected.textContent : "当前 Plan Version";
    if (!window.confirm("删除 " + name + "？已被会议或议题使用的版本不会被删除。")) return;
    fetch("/plan-versions/" + encodeURIComponent(versionSelect.value), { method: "DELETE" })
      .then(parseJson)
      .then(function () {
        if (selected) selected.remove();
        if (versionSelect.options.length) {
          versionSelect.selectedIndex = 0;
          refreshRounds(versionSelect);
        }
      })
      .catch(function (error) { window.alert(error.message); });
  }

  function deleteRound(versionSelect) {
    var roundSelect = roundSelectNear(versionSelect);
    if (!roundSelect || !roundSelect.value) return;
    var selected = roundSelect.options[roundSelect.selectedIndex];
    var name = selected ? selected.textContent : "当前 Round";
    if (!window.confirm("删除 " + name + "？已被会议或议题使用的 Round 不会被删除。")) return;
    fetch("/plan-rounds/" + encodeURIComponent(roundSelect.value), { method: "DELETE" })
      .then(parseJson)
      .then(function () {
        if (selected) selected.remove();
        if (roundSelect.options.length) {
          roundSelect.selectedIndex = 0;
        } else {
          refreshRounds(versionSelect);
        }
      })
      .catch(function (error) { window.alert(error.message); });
  }

  function isFilterSelect(select) {
    return Boolean(select.closest(".meeting-filter-grid, .topic-draft-filter-grid"));
  }

  function controlGroupFor(select) {
    var parent = select.parentElement;
    if (parent && parent.classList.contains("plan-dimension-control")) {
      return parent;
    }
    var group = document.createElement("div");
    group.className = "plan-dimension-control";
    select.insertAdjacentElement("beforebegin", group);
    group.appendChild(select);
    return group;
  }

  function addButton(select, label, title, handler, variant) {
    if (select.disabled) return;
    var group = controlGroupFor(select);
    var button = document.createElement("button");
    button.type = "button";
    button.className = "plan-dimension-action" + (variant ? " " + variant : "");
    button.setAttribute("aria-label", title);
    button.setAttribute("title", title);
    button.textContent = label;
    button.addEventListener("click", handler);
    group.appendChild(button);
  }

  window.AppShell.ready(function () {
    document.querySelectorAll("[data-plan-version-select]").forEach(function (select) {
      select.addEventListener("change", function () { refreshRounds(select); });
      if (select.closest("form") && !isFilterSelect(select) && window.AppShell) {
        addButton(select, "+", "新增 Plan Version", function () { createPlanVersion(select); }, "plan-dimension-add");
        addButton(select, "×", "删除当前 Plan Version", function () { deletePlanVersion(select); }, "plan-dimension-delete");
      }
    });
    document.querySelectorAll("[data-plan-round-select]").forEach(function (select) {
      var form = select.closest("form") || document;
      var versionSelect = form.querySelector("[data-plan-version-select]");
      if (versionSelect && !isFilterSelect(select)) {
        addButton(select, "+", "新增 Round", function () { createRound(versionSelect); }, "plan-dimension-add");
        addButton(select, "×", "删除当前 Round", function () { deleteRound(versionSelect); }, "plan-dimension-delete");
      }
    });
  });
})();
