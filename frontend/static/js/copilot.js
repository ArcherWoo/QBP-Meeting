(function () {
    var root = document.getElementById("ai-copilot");
    if (!root) {
        return;
    }

    var launcher = document.getElementById("ai-copilot-launcher");
    var closeButton = document.getElementById("ai-copilot-close");
    var panel = document.getElementById("ai-copilot-panel");
    var statusLabel = document.getElementById("ai-copilot-status");
    var modelSelect = document.getElementById("ai-copilot-model");
    var input = document.getElementById("ai-copilot-input");
    var sendButton = document.getElementById("ai-copilot-send");
    var clearButton = document.getElementById("ai-copilot-clear");
    var messages = document.getElementById("ai-copilot-messages");
    var emptyState = document.getElementById("ai-copilot-empty");
    var contextBox = document.getElementById("ai-copilot-context");
    var mentionMenu = document.getElementById("ai-copilot-mention-menu");
    var settingsToggle = document.getElementById("ai-copilot-settings-toggle");
    var settingsPopover = document.getElementById("ai-copilot-settings-popover");
    var currentMeeting = getData(root, "copilot-current-meeting") || "";
    var currentTopic = getData(root, "copilot-current-topic") || "";
    var pageMeetingNos = parsePageMeetings(getData(root, "copilot-page-meetings"));
    var pageContext = getData(root, "copilot-page-context") || "";
    var pageKey = getData(root, "copilot-page-key") || "global";
    var pageContextLabel = getData(root, "copilot-page-label") || "本页会议";
    var storageKey = copilotStorageKey();
    var referencedMeetings = [];
    var mentionRequestId = 0;
    var restored = false;
    var statusLoaded = false;
    var copilotAvailable = true;

    function getData(node, name) {
        return node ? node.getAttribute("data-" + name) : "";
    }

    function addEvent(node, eventName, handler) {
        if (!node) {
            return;
        }
        if (node.addEventListener) {
            node.addEventListener(eventName, handler, false);
        } else if (node.attachEvent) {
            node.attachEvent("on" + eventName, handler);
        }
    }

    function hasClass(node, className) {
        return node && (" " + node.className + " ").indexOf(" " + className + " ") >= 0;
    }

    function addClass(node, className) {
        if (node && !hasClass(node, className)) {
            node.className = trim(node.className + " " + className);
        }
    }

    function removeClass(node, className) {
        if (node) {
            node.className = trim((" " + node.className + " ").replace(" " + className + " ", " "));
        }
    }

    function setRawContent(node, content) {
        node._rawContent = content || "";
    }

    function getRawContent(target) {
        return target ? (target._rawContent || "") : "";
    }

    function trim(value) {
        return String(value || "").replace(/^\s+|\s+$/g, "");
    }

    function syncPageContext() {
        var nextStorageKey;
        currentMeeting = getData(root, "copilot-current-meeting") || "";
        currentTopic = getData(root, "copilot-current-topic") || "";
        pageMeetingNos = parsePageMeetings(getData(root, "copilot-page-meetings"));
        pageContext = getData(root, "copilot-page-context") || "";
        pageKey = getData(root, "copilot-page-key") || "global";
        pageContextLabel = getData(root, "copilot-page-label") || "本页会议";
        nextStorageKey = copilotStorageKey();
        if (nextStorageKey !== storageKey) {
            storageKey = nextStorageKey;
            referencedMeetings = [];
            restored = false;
            if (hasClass(root, "open")) {
                removeRenderedMessages();
                restoreMessages();
            }
        }
        if (!hasClass(root, "open")) {
            restored = false;
        }
        renderContext();
    }

    function copilotStorageKey() {
        if (currentMeeting) {
            return "qbp-copilot:meeting:" + currentMeeting;
        }
        if (pageKey) {
            return "qbp-copilot:page:" + pageKey;
        }
        return "qbp-copilot:global";
    }

    function openPanel() {
        addClass(root, "open");
        panel.setAttribute("aria-hidden", "false");
        restoreMessages();
        loadStatus();
        renderContext();
        try {
            input.focus();
        } catch (error) {
            ignore(error);
        }
    }

    function closePanel() {
        removeClass(root, "open");
        panel.setAttribute("aria-hidden", "true");
        removeClass(mentionMenu, "open");
    }

    function setStatus(text, state) {
        statusLabel.className = trim("ai-copilot-status " + (state || ""));
        statusLabel.innerHTML = svgIcon("status-dot", "icon-status-dot") + " " + escapeHtml(text);
    }

    function renderContext() {
        contextBox.innerHTML = "";
        if (currentMeeting) {
            contextBox.appendChild(contextChip("当前 " + currentMeeting, "locked"));
        } else if (pageMeetingNos.length) {
            contextBox.appendChild(contextChip(pageContextLabel + " " + pageMeetingNos.length + " 个", "locked"));
        }
        each(referencedMeetings, function (meeting) {
            var chip = contextChip(meeting.meeting_no, "removable");
            var button = chip.getElementsByTagName("button")[0];
            addEvent(button, "click", function () {
                var index = indexOfMeeting(meeting.meeting_no);
                if (index >= 0) {
                    referencedMeetings.splice(index, 1);
                }
                renderContext();
            });
            contextBox.appendChild(chip);
        });
        if (!currentMeeting && pageMeetingNos.length === 0 && referencedMeetings.length === 0) {
            contextBox.innerHTML = '<span class="ai-context-empty">输入 @ 引用会议</span>';
        }
    }

    function contextChip(label, mode) {
        var chip = document.createElement("span");
        chip.className = "ai-context-chip " + (mode || "");
        chip.innerHTML = svgIcon("link", "icon-small") + "<span>" + escapeHtml(label) + "</span>";
        if (mode === "removable") {
            chip.innerHTML += '<button type="button" aria-label="移除引用">' + svgIcon("close", "icon-small") + "</button>";
        }
        return chip;
    }

    function svgIcon(name, className) {
        var paths = {
            "status-dot": '<circle cx="12" cy="12" r="5"></circle>',
            link: '<path d="M8.5 17.5a5 5 0 0 1 0-7.1l2.2-2.1"></path><path d="M13.3 15.7l2.2-2.1a2 2 0 0 0-2.8-2.8"></path><path d="M10 14l4-4"></path>',
            close: '<path d="M6 6l12 12M18 6L6 18"></path>'
        };
        return '<span class="svg-icon ' + (className || "") + '" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false">' + (paths[name] || paths["status-dot"]) + "</svg></span>";
    }

    function parsePageMeetings(raw) {
        var parsed;
        try {
            parsed = JSON.parse(raw || "[]");
        } catch (error) {
            return [];
        }
        if (!isArray(parsed)) {
            return [];
        }
        var values = [];
        each(parsed, function (item) {
            if (item && values.length < 10) {
                values.push(item);
            }
        });
        return values;
    }

    function loadStatus() {
        if (statusLoaded) {
            return;
        }
        statusLoaded = true;
        xhrJson("GET", "/copilot/status", null, function (data) {
            if (!data.api_key_configured) {
                copilotAvailable = false;
                modelSelect.innerHTML = '<option value="">未配置智枢 API Key</option>';
                modelSelect.title = "请在 .env 配置 ZHISHU_API_KEY";
                sendButton.disabled = true;
                setStatus("待配置", "error");
                return;
            }
            copilotAvailable = true;
            sendButton.disabled = false;
            loadModels();
        }, function (message) {
            copilotAvailable = false;
            modelSelect.innerHTML = '<option value="">状态检查失败</option>';
            modelSelect.title = message;
            sendButton.disabled = true;
            setStatus("检查失败", "error");
        });
    }

    function loadModels() {
        if (getData(modelSelect, "loaded") === "1") {
            return;
        }
        setStatus("连接中", "loading");
        modelSelect.innerHTML = '<option value="">加载模型...</option>';
        xhrJson("GET", "/copilot/models", null, function (data) {
            var models = data.models || [];
            var html = "";
            each(models, function (model) {
                var id = model.id || "";
                var name = model.name || id;
                var selected = id === data.default_model ? " selected" : "";
                html += '<option value="' + escapeAttr(id) + '"' + selected + ">" + escapeHtml(name) + "</option>";
            });
            modelSelect.innerHTML = html || '<option value="">无可用模型</option>';
            modelSelect.setAttribute("data-loaded", "1");
            setStatus("已连接", "online");
        }, function (message) {
            modelSelect.innerHTML = '<option value="">未连接智枢</option>';
            modelSelect.title = message;
            setStatus("连接失败", "error");
        });
    }

    function searchMeetings(query, callback) {
        xhrJson("GET", "/copilot/context/search?q=" + encodeURIComponent(query || ""), null, function (data) {
            callback(data.results || []);
        }, function () {
            callback([]);
        });
    }

    function currentMentionQuery() {
        var cursor = typeof input.selectionStart === "number" ? input.selectionStart : input.value.length;
        var before = input.value.slice(0, cursor);
        var at = before.lastIndexOf("@");
        var query;
        if (at < 0) {
            return null;
        }
        query = before.slice(at + 1);
        if (/\s/.test(query)) {
            return null;
        }
        return { at: at, query: query, cursor: cursor };
    }

    function updateMentionMenu() {
        var mention = currentMentionQuery();
        var requestId;
        var query;
        if (!mention) {
            removeClass(mentionMenu, "open");
            return;
        }
        requestId = ++mentionRequestId;
        var query = mention.query;
        mentionMenu.innerHTML = query.length === 0
            ? '<div class="ai-mention-loading">最近会议</div>'
            : '<div class="ai-mention-loading">搜索中...</div>';
        addClass(mentionMenu, "open");
        searchMeetings(query, function (results) {
            if (requestId !== mentionRequestId) {
                return;
            }
            renderMentionResults(results, mention);
        });
    }

    function renderMentionResults(results, mention) {
        mentionMenu.innerHTML = "";
        if (!results.length) {
            mentionMenu.innerHTML = '<div class="ai-mention-loading">没有匹配会议</div>';
            return;
        }
        each(results, function (meeting) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "ai-mention-item";
            button.innerHTML =
                "<strong>" + escapeHtml(meeting.meeting_no) + "</strong>" +
                "<span>" + escapeHtml(meeting.title) + "</span>" +
                "<small>" + escapeHtml(meeting.meeting_date || "") + " · " + (meeting.topic_count || 0) + " topics</small>";
            addEvent(button, "click", function () {
                selectMention(meeting, mention);
            });
            mentionMenu.appendChild(button);
        });
    }

    function selectMention(meeting, mention) {
        if (referencedMeetings.length >= 5 && indexOfMeeting(meeting.meeting_no) < 0) {
            appendMessage("assistant", "最多引用 5 个会议。");
            return;
        }
        if (indexOfMeeting(meeting.meeting_no) < 0) {
            referencedMeetings.push(meeting);
        }
        input.value = input.value.slice(0, mention.at) + input.value.slice(mention.cursor);
        removeClass(mentionMenu, "open");
        renderContext();
        input.focus();
    }

    function appendMessage(role, content) {
        var node = document.createElement("div");
        if (emptyState) {
            emptyState.style.display = "none";
        }
        node.className = "ai-message " + role;
        setRawContent(node, content || "");
        if (role === "assistant") {
            renderAssistantMessage(node);
        } else {
            node.textContent = content || "";
        }
        messages.appendChild(node);
        messages.scrollTop = messages.scrollHeight;
        persistMessages();
        return node;
    }

    function persistMessages() {
        var nodes;
        var history = [];
        try {
            nodes = messages.querySelectorAll(".ai-message");
            eachNode(nodes, function (node) {
                history.push({
                    role: hasClass(node, "user") ? "user" : "assistant",
                    content: getRawContent(node) || node.textContent || ""
                });
            });
            history = history.slice(-30);
            window.sessionStorage.setItem(storageKey, JSON.stringify(history));
        } catch (error) {
            ignore(error);
        }
    }

    function restoreMessages() {
        var history;
        if (restored) {
            return;
        }
        restored = true;
        try {
            history = JSON.parse(window.sessionStorage.getItem(storageKey) || "[]");
            if (!isArray(history) || history.length === 0) {
                return;
            }
            if (emptyState) {
                emptyState.style.display = "none";
            }
            each(history, function (item) {
                var node = document.createElement("div");
                var role = item.role === "user" ? "user" : "assistant";
                node.className = "ai-message " + role;
                setRawContent(node, item.content || "");
                if (role === "user") {
                    node.textContent = item.content || "";
                } else {
                    renderAssistantMessage(node);
                }
                messages.appendChild(node);
            });
            messages.scrollTop = messages.scrollHeight;
        } catch (error) {
            try {
                window.sessionStorage.removeItem(storageKey);
            } catch (storageError) {
                ignore(storageError);
            }
        }
    }

    function clearMessages() {
        removeRenderedMessages();
        try {
            window.sessionStorage.removeItem(storageKey);
        } catch (error) {
            ignore(error);
        }
    }

    function removeRenderedMessages() {
        var nodes = messages.querySelectorAll(".ai-message");
        for (var index = nodes.length - 1; index >= 0; index -= 1) {
            nodes[index].parentNode.removeChild(nodes[index]);
        }
        if (emptyState) {
            emptyState.style.display = "";
        }
    }

    function buildChatPayload(message) {
        return {
            message: message,
            model: modelSelect.value,
            current_meeting_no: currentMeeting,
            current_topic_id: currentTopic,
            page_context: pageContext,
            page_meeting_nos: pageMeetingNos,
            referenced_meeting_nos: referencedMeetingNos()
        };
    }

    function sendMessage() {
        var message = trim(input.value);
        var assistant;
        var payload;
        if (!message || sendButton.disabled || !copilotAvailable) {
            return;
        }
        input.value = "";
        appendMessage("user", message);
        assistant = appendMessage("assistant", "");
        payload = buildChatPayload(message);
        sendButton.disabled = true;
        setStatus("生成中", "loading");

        if (supportsStreaming()) {
            xhrStream("/copilot/chat/stream", payload, function (chunk) {
                appendSseChunk(chunk, assistant);
            }, function () {
                finishAssistantMessage(assistant);
            }, function (messageText) {
                failAssistantMessage(assistant, messageText);
            });
        } else {
            xhrJson("POST", "/copilot/chat", payload, function (data) {
                setRawContent(assistant, data.answer || "");
                finishAssistantMessage(assistant);
            }, function (messageText) {
                failAssistantMessage(assistant, messageText);
            });
        }
    }

    function finishAssistantMessage(assistant) {
        if (!trim(getRawContent(assistant))) {
            setRawContent(assistant, "没有收到内容。");
        }
        renderAssistantMessage(assistant);
        persistMessages();
        setStatus("已连接", "online");
        sendButton.disabled = false;
    }

    function failAssistantMessage(assistant, messageText) {
        setRawContent(assistant, messageText || "Copilot 请求失败");
        renderAssistantMessage(assistant);
        persistMessages();
        setStatus("请求失败", "error");
        sendButton.disabled = false;
    }

    function supportsStreaming() {
        return !isIE() && !!window.XMLHttpRequest;
    }

    function isIE() {
        var ua = window.navigator ? window.navigator.userAgent : "";
        return ua.indexOf("MSIE ") >= 0 || ua.indexOf("Trident/") >= 0;
    }

    function xhrJson(method, url, body, onSuccess, onError) {
        var xhr = new XMLHttpRequest();
        xhr.open(method, url, true);
        xhr.setRequestHeader("Accept", "application/json");
        if (body) {
            xhr.setRequestHeader("Content-Type", "application/json");
        }
        xhr.onreadystatechange = function () {
            var data;
            if (xhr.readyState !== 4) {
                return;
            }
            try {
                data = xhr.responseText ? JSON.parse(xhr.responseText) : {};
            } catch (error) {
                data = {};
            }
            if (xhr.status >= 200 && xhr.status < 300) {
                onSuccess(data);
            } else {
                onError((data && data.error) || ("HTTP " + xhr.status));
            }
        };
        xhr.send(body ? JSON.stringify(body) : null);
    }

    function xhrStream(url, body, onChunk, onDone, onError) {
        var xhr = new XMLHttpRequest();
        var seen = 0;
        var buffer = "";
        xhr.open("POST", url, true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onreadystatechange = function () {
            if (xhr.readyState === 3 || xhr.readyState === 4) {
                readProgress();
            }
            if (xhr.readyState === 4) {
                if (xhr.status >= 200 && xhr.status < 300) {
                    if (trim(buffer)) {
                        onChunk(buffer);
                    }
                    onDone();
                } else {
                    onError(parseError(xhr.responseText, xhr.status));
                }
            }
        };
        xhr.onprogress = readProgress;
        function readProgress() {
            var text = xhr.responseText || "";
            var next;
            var parts;
            if (text.length <= seen) {
                return;
            }
            next = text.substring(seen);
            seen = text.length;
            buffer += next;
            parts = buffer.split("\n\n");
            buffer = parts.pop() || "";
            each(parts, function (part) {
                onChunk(part);
            });
        }
        xhr.send(JSON.stringify(body));
    }

    function parseError(text, status) {
        var data;
        try {
            data = JSON.parse(text || "{}");
        } catch (error) {
            data = {};
        }
        return data.error || ("HTTP " + status);
    }

    function appendSseChunk(chunk, target) {
        var lines = String(chunk || "").split("\n");
        each(lines, function (line) {
            var trimmed = trim(line);
            var data;
            var parsed;
            var content = "";
            if (!trimmed) {
                return;
            }
            data = trimmed.indexOf("data:") === 0 ? trim(trimmed.slice(5)) : trimmed;
            if (!data || data === "[DONE]") {
                return;
            }
            try {
                parsed = JSON.parse(data);
                if (parsed.choices && parsed.choices[0]) {
                    if (parsed.choices[0].delta && parsed.choices[0].delta.content) {
                        content = parsed.choices[0].delta.content;
                    } else if (parsed.choices[0].message && parsed.choices[0].message.content) {
                        content = parsed.choices[0].message.content;
                    }
                }
                if (content) {
                    setRawContent(target, getRawContent(target) + content);
                }
                if (parsed.error) {
                    setRawContent(target, getRawContent(target) + parsed.error);
                }
            } catch (error) {
                setRawContent(target, getRawContent(target) + data);
            }
            renderAssistantMessage(target);
            messages.scrollTop = messages.scrollHeight;
        });
    }

    function renderAssistantMessage(node) {
        node.innerHTML = '<div class="ai-message-markdown">' + renderMarkdown(getRawContent(node) || "") + "</div>";
    }

    function renderMarkdown(markdown) {
        var lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
        var blocks = [];
        var paragraph = [];
        var list = [];
        var inCode = false;
        var codeLines = [];
        var index;
        var codeFence = String.fromCharCode(96) + String.fromCharCode(96) + String.fromCharCode(96);
        var line;
        var trimmed;
        var table;
        var heading;
        var listMatch;

        function flushParagraph() {
            if (!paragraph.length) {
                return;
            }
            blocks.push("<p>" + renderInline(paragraph.join(" ")) + "</p>");
            paragraph = [];
        }

        function flushList() {
            var html = "";
            if (!list.length) {
                return;
            }
            each(list, function (item) {
                html += "<li>" + renderInline(item) + "</li>";
            });
            blocks.push("<ul>" + html + "</ul>");
            list = [];
        }

        function flushCode() {
            if (!codeLines.length) {
                return;
            }
            blocks.push("<pre><code>" + escapeHtml(codeLines.join("\n")) + "</code></pre>");
            codeLines = [];
        }

        for (index = 0; index < lines.length; index += 1) {
            line = lines[index];
            trimmed = trim(line);

            if (trimmed.indexOf(codeFence) === 0) {
                if (inCode) {
                    flushCode();
                    inCode = false;
                } else {
                    flushParagraph();
                    flushList();
                    inCode = true;
                }
                continue;
            }

            if (inCode) {
                codeLines.push(line);
                continue;
            }

            table = readMarkdownTable(lines, index);
            if (table) {
                flushParagraph();
                flushList();
                blocks.push(renderMarkdownTable(table.rows));
                index = table.endIndex;
                continue;
            }

            if (!trimmed || trimmed === "---") {
                flushParagraph();
                flushList();
                continue;
            }

            heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
            if (heading) {
                flushParagraph();
                flushList();
                blocks.push("<h" + Math.min(heading[1].length + 2, 5) + ">" + renderInline(heading[2]) + "</h" + Math.min(heading[1].length + 2, 5) + ">");
                continue;
            }

            listMatch = trimmed.match(/^[-*]\s+(.+)$/) || trimmed.match(/^\d+\.\s+(.+)$/);
            if (listMatch) {
                flushParagraph();
                list.push(listMatch[1]);
                continue;
            }

            paragraph.push(trimmed);
        }

        flushCode();
        flushParagraph();
        flushList();
        return blocks.join("");
    }

    function readMarkdownTable(lines, startIndex) {
        var rows;
        var index;
        if (!isTableRow(lines[startIndex]) || !isSeparatorRow(lines[startIndex + 1] || "")) {
            return null;
        }
        rows = [splitTableRow(lines[startIndex])];
        index = startIndex + 2;
        while (index < lines.length && isTableRow(lines[index])) {
            rows.push(splitTableRow(lines[index]));
            index += 1;
        }
        return { rows: rows, endIndex: index - 1 };
    }

    function renderMarkdownTable(rows) {
        var headers = rows[0] || [];
        var thead = "";
        var tbody = "";
        each(headers, function (cell) {
            thead += "<th>" + renderInline(cell) + "</th>";
        });
        for (var rowIndex = 1; rowIndex < rows.length; rowIndex += 1) {
            tbody += "<tr>";
            for (var colIndex = 0; colIndex < headers.length; colIndex += 1) {
                tbody += "<td>" + renderInline(rows[rowIndex][colIndex] || "") + "</td>";
            }
            tbody += "</tr>";
        }
        return '<div class="ai-message-table-wrap"><table><thead><tr>' + thead + "</tr></thead><tbody>" + tbody + "</tbody></table></div>";
    }

    function isTableRow(line) {
        var value = trim(line);
        return typeof line === "string" && value.charAt(0) === "|" && value.charAt(value.length - 1) === "|" && value.indexOf("|") >= 0;
    }

    function isSeparatorRow(line) {
        var cells;
        var ok = true;
        if (!isTableRow(line)) {
            return false;
        }
        cells = splitTableRow(line);
        each(cells, function (cell) {
            if (!/^:?-{3,}:?$/.test(trim(cell))) {
                ok = false;
            }
        });
        return ok;
    }

    function splitTableRow(line) {
        var cells = trim(line).replace(/^\|/, "").replace(/\|$/, "").split("|");
        var result = [];
        each(cells, function (cell) {
            result.push(trim(cell));
        });
        return result;
    }

    function renderInline(value) {
        var html = escapeHtml(value);
        var tick = String.fromCharCode(96);
        html = html.replace(new RegExp(tick + "([^" + tick + "]+)" + tick, "g"), "<code>$1</code>");
        html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
        html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
        return html;
    }

    function referencedMeetingNos() {
        var values = [];
        each(referencedMeetings, function (meeting) {
            values.push(meeting.meeting_no);
        });
        return values;
    }

    function indexOfMeeting(meetingNo) {
        for (var index = 0; index < referencedMeetings.length; index += 1) {
            if (referencedMeetings[index].meeting_no === meetingNo) {
                return index;
            }
        }
        return -1;
    }

    function each(items, callback) {
        for (var index = 0; index < items.length; index += 1) {
            callback(items[index], index);
        }
    }

    function eachNode(nodes, callback) {
        for (var index = 0; index < nodes.length; index += 1) {
            callback(nodes[index], index);
        }
    }

    function isArray(value) {
        return Object.prototype.toString.call(value) === "[object Array]";
    }

    function escapeHtml(value) {
        return String(value || "").replace(/[&<>"']/g, function (char) {
            return {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#039;"
            }[char];
        });
    }

    function escapeAttr(value) {
        return escapeHtml(value).replace(new RegExp(String.fromCharCode(96), "g"), "&#096;");
    }

    function ignore(value) {
        return value;
    }

    addEvent(document, "click", function (event) {
        var target = event.target;
        var button = target && target.closest ? target.closest("[data-copilot-open]") : null;
        if (!button) {
            return;
        }
        if (event.preventDefault) {
            event.preventDefault();
        } else {
            event.returnValue = false;
        }
        openPanel();
    });
    addEvent(closeButton, "click", closePanel);
    addEvent(sendButton, "click", sendMessage);
    addEvent(clearButton, "click", clearMessages);
    if (settingsToggle && settingsPopover) {
        addEvent(settingsToggle, "click", function (event) {
            event.stopPropagation();
            var isOpen = !settingsPopover.hasAttribute("hidden");
            if (isOpen) {
                settingsPopover.setAttribute("hidden", "");
                removeClass(settingsToggle, "open");
                settingsToggle.setAttribute("aria-expanded", "false");
            } else {
                settingsPopover.removeAttribute("hidden");
                addClass(settingsToggle, "open");
                settingsToggle.setAttribute("aria-expanded", "true");
            }
        });
        addEvent(document, "click", function (event) {
            if (settingsPopover.hasAttribute("hidden")) {
                return;
            }
            if (settingsPopover.contains(event.target) || settingsToggle.contains(event.target)) {
                return;
            }
            settingsPopover.setAttribute("hidden", "");
            removeClass(settingsToggle, "open");
            settingsToggle.setAttribute("aria-expanded", "false");
        });
    }
    eachNode(root.querySelectorAll(".ai-copilot-quick-prompts button"), function (button) {
        addEvent(button, "click", function () {
            input.value = getData(button, "prompt") || "";
            input.focus();
        });
    });
    addEvent(input, "input", updateMentionMenu);
    addEvent(input, "keyup", function () {
        updateMentionMenu();
    });
    addEvent(input, "keydown", function (event) {
        event = event || window.event;
        if ((event.key === "Enter" || event.keyCode === 13) && !event.shiftKey) {
            if (event.preventDefault) {
                event.preventDefault();
            } else {
                event.returnValue = false;
            }
            sendMessage();
        }
    });
    window.QBPCopilot = {
        refreshContext: syncPageContext
    };
}());
