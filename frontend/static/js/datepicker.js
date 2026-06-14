// 离线日历控件 — 把 <input data-datepicker> 文本输入框变成 YYYY-MM-DD 日历选择器
// 不依赖任何外部库，兼容 Edge / Chrome / IE11+，显示格式跨浏览器/系统语言一致。
(function () {
    var WEEK_LABELS = ["一", "二", "三", "四", "五", "六", "日"];
    var MONTH_LABEL = function (y, m) { return y + " 年 " + (m + 1) + " 月"; };
    var activePanel = null;

    function pad(n) { return n < 10 ? "0" + n : "" + n; }
    function fmt(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
    function parseISO(s) {
        if (!s) return null;
        var m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s.trim());
        if (!m) return null;
        var d = new Date(parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10));
        return isNaN(d.getTime()) ? null : d;
    }

    function closeAll() {
        if (activePanel) {
            activePanel.panel.parentNode && activePanel.panel.parentNode.removeChild(activePanel.panel);
            activePanel = null;
        }
    }

    function buildGrid(panel, input, viewYear, viewMonth) {
        var body = panel.querySelector(".dp-body");
        var header = panel.querySelector(".dp-header-title");
        header.textContent = MONTH_LABEL(viewYear, viewMonth);
        body.innerHTML = "";
        var head = document.createElement("div"); head.className = "dp-week";
        WEEK_LABELS.forEach(function (w) { var c = document.createElement("span"); c.textContent = w; head.appendChild(c); });
        body.appendChild(head);
        var grid = document.createElement("div"); grid.className = "dp-grid";
        var first = new Date(viewYear, viewMonth, 1);
        var startWeekday = (first.getDay() + 6) % 7; // 周一=0
        var prevTail = new Date(viewYear, viewMonth, 0).getDate();
        var daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
        var today = fmt(new Date());
        var selected = input.value;
        var cells = [];
        for (var i = 0; i < startWeekday; i++) cells.push({ day: prevTail - startWeekday + 1 + i, muted: true, y: viewYear, m: viewMonth - 1 });
        for (var d = 1; d <= daysInMonth; d++) cells.push({ day: d, muted: false, y: viewYear, m: viewMonth });
        while (cells.length % 7 !== 0) cells.push({ day: cells.length - startWeekday - daysInMonth + 1, muted: true, y: viewYear, m: viewMonth + 1 });
        cells.forEach(function (c) {
            var btn = document.createElement("button");
            btn.type = "button"; btn.className = "dp-day"; btn.textContent = c.day;
            var realDate = new Date(c.y, c.m, c.day);
            var iso = fmt(realDate);
            if (c.muted) btn.classList.add("dp-muted");
            if (iso === today) btn.classList.add("dp-today");
            if (iso === selected) btn.classList.add("dp-selected");
            btn.addEventListener("click", function (ev) {
                ev.stopPropagation();
                input.value = iso;
                input.dispatchEvent(new Event("input", { bubbles: true }));
                input.dispatchEvent(new Event("change", { bubbles: true }));
                closeAll();
            });
            grid.appendChild(btn);
        });
        body.appendChild(grid);
    }

    function openPanel(input) {
        closeAll();
        var panel = document.createElement("div"); panel.className = "dp-panel";
        panel.innerHTML = '<div class="dp-header">' +
            '<button type="button" class="dp-nav" data-step="-12" aria-label="上一年">«</button>' +
            '<button type="button" class="dp-nav" data-step="-1" aria-label="上一月">‹</button>' +
            '<span class="dp-header-title"></span>' +
            '<button type="button" class="dp-nav" data-step="1" aria-label="下一月">›</button>' +
            '<button type="button" class="dp-nav" data-step="12" aria-label="下一年">»</button>' +
            '</div><div class="dp-body"></div>' +
            '<div class="dp-footer"><button type="button" class="dp-action" data-action="today">今天</button>' +
            '<button type="button" class="dp-action" data-action="clear">清除</button></div>';
        document.body.appendChild(panel);
        var current = parseISO(input.value) || new Date();
        var view = { y: current.getFullYear(), m: current.getMonth() };
        function render() { buildGrid(panel, input, view.y, view.m); }
        render();
        panel.addEventListener("click", function (ev) { ev.stopPropagation(); });
        Array.prototype.forEach.call(panel.querySelectorAll(".dp-nav"), function (b) {
            b.addEventListener("click", function () {
                var step = parseInt(b.getAttribute("data-step"), 10);
                var d = new Date(view.y, view.m + step, 1);
                view.y = d.getFullYear(); view.m = d.getMonth(); render();
            });
        });
        panel.querySelector('[data-action="today"]').addEventListener("click", function () {
            var t = new Date(); input.value = fmt(t);
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
            closeAll();
        });
        panel.querySelector('[data-action="clear"]').addEventListener("click", function () {
            input.value = "";
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
            closeAll();
        });
        var rect = input.getBoundingClientRect();
        var top = rect.bottom + window.scrollY + 4;
        var left = rect.left + window.scrollX;
        panel.style.top = top + "px"; panel.style.left = left + "px";
        var pw = panel.offsetWidth || 240;
        if (left + pw > window.scrollX + document.documentElement.clientWidth - 8) {
            panel.style.left = Math.max(8, window.scrollX + document.documentElement.clientWidth - pw - 8) + "px";
        }
        activePanel = { panel: panel, input: input };
    }

    function bind(input) {
        if (input.__dpBound) return;
        input.__dpBound = true;
        input.setAttribute("autocomplete", "off");
        if (!input.getAttribute("placeholder")) input.setAttribute("placeholder", "YYYY-MM-DD");
        input.addEventListener("focus", function () { openPanel(input); });
        input.addEventListener("click", function (ev) { ev.stopPropagation(); openPanel(input); });
        input.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape") closeAll();
        });
    }

    function scan(root) {
        var nodes = (root || document).querySelectorAll("input[data-datepicker]");
        Array.prototype.forEach.call(nodes, bind);
    }

    document.addEventListener("click", function () { closeAll(); });
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closeAll(); });
    window.addEventListener("scroll", closeAll, true);
    window.addEventListener("resize", closeAll);

    window.AppDatepicker = { scan: scan, open: openPanel, close: closeAll };
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () { scan(); });
    } else {
        scan();
    }
})();
