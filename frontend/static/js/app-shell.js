(function () {
    var rootSelector = "[data-soft-nav-root]";
    var sidebarSelector = "[data-soft-nav-sidebar]";
    var currentController = null;

    function getRoot() {
        return document.querySelector(rootSelector);
    }

    function sameOrigin(url) {
        return url.origin === window.location.origin;
    }

    function isSoftNavigableLink(link) {
        var url;
        if (!link || !link.href) return false;
        if (link.target && link.target !== "_self") return false;
        if (link.hasAttribute("download") || link.hasAttribute("data-no-soft-nav")) return false;
        if (link.closest("[data-no-soft-nav]")) return false;
        url = new URL(link.href, window.location.href);
        if (!sameOrigin(url)) return false;
        if (url.hash && url.pathname === window.location.pathname && url.search === window.location.search) return false;
        if (url.pathname.indexOf("/auth/logout") === 0) return false;
        if (url.pathname.indexOf("/attachments/") === 0) return false;
        return true;
    }

    function setBusy(isBusy) {
        var root = getRoot();
        if (!root) return;
        root.classList.toggle("soft-nav-loading", !!isBusy);
        root.setAttribute("aria-busy", isBusy ? "true" : "false");
    }

    function isGlobalScript(script) {
        var src = script.getAttribute("src") || "";
        return src.indexOf("/static/js/app-shell.js") >= 0
            || src.indexOf("/static/js/datepicker.js") >= 0
            || src.indexOf("/static/js/copilot.js") >= 0;
    }

    function runScript(oldScript, replaceInPlace) {
        var script = document.createElement("script");
        Array.prototype.forEach.call(oldScript.attributes, function (attr) {
            script.setAttribute(attr.name, attr.value);
        });
        script.text = oldScript.text || oldScript.textContent || "";
        if (replaceInPlace && oldScript.parentNode) {
            oldScript.parentNode.replaceChild(script, oldScript);
        } else {
            document.body.appendChild(script);
            document.body.removeChild(script);
        }
    }

    function executeScripts(container, sourceDoc) {
        var scripts = Array.prototype.slice.call(container.querySelectorAll("script"));
        var sourceRoot = sourceDoc ? sourceDoc.querySelector(rootSelector) : null;
        var extraScripts = sourceDoc
            ? Array.prototype.slice.call(sourceDoc.body.querySelectorAll("script")).filter(function (script) {
                return !isGlobalScript(script) && !(sourceRoot && sourceRoot.contains(script));
            })
            : [];
        scripts.forEach(function (oldScript) {
            if (isGlobalScript(oldScript)) return;
            var script = document.createElement("script");
            Array.prototype.forEach.call(oldScript.attributes, function (attr) {
                script.setAttribute(attr.name, attr.value);
            });
            script.text = oldScript.text || oldScript.textContent || "";
            oldScript.parentNode.replaceChild(script, oldScript);
        });
        extraScripts.forEach(function (script) {
            runScript(script, false);
        });
    }

    function updateDocumentFrom(html, url) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, "text/html");
        var nextRoot = doc.querySelector(rootSelector);
        var currentRoot = getRoot();
        var nextSidebar = doc.querySelector(sidebarSelector);
        var currentSidebar = document.querySelector(sidebarSelector);
        var nextUserInfo = doc.querySelector(".user-info");
        var currentUserInfo = document.querySelector(".user-info");
        var nextCopilot = doc.getElementById("ai-copilot");
        var currentCopilot = document.getElementById("ai-copilot");

        if (!nextRoot || !currentRoot) {
            window.location.href = url;
            return;
        }
        if (currentSidebar && !nextSidebar) {
            window.location.href = url;
            return;
        }

        document.title = doc.title || document.title;
        currentRoot.replaceWith(nextRoot);
        if (nextSidebar && currentSidebar) {
            currentSidebar.innerHTML = nextSidebar.innerHTML;
        }
        if (nextUserInfo && currentUserInfo) {
            currentUserInfo.innerHTML = nextUserInfo.innerHTML;
        }
        if (nextCopilot && currentCopilot) {
            Array.prototype.forEach.call(nextCopilot.attributes, function (attr) {
                currentCopilot.setAttribute(attr.name, attr.value);
            });
            if (window.QBPCopilot && window.QBPCopilot.refreshContext) {
                window.QBPCopilot.refreshContext();
            }
        }
        executeScripts(nextRoot, doc);
        if (window.AppDatepicker && window.AppDatepicker.scan) {
            window.AppDatepicker.scan(nextRoot);
        }
        window.dispatchEvent(new CustomEvent("qbp:page-ready", { detail: { url: url } }));
        window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    }

    function navigate(url, pushHistory) {
        if (currentController) {
            currentController.abort();
        }
        currentController = new AbortController();
        setBusy(true);
        fetch(url, {
            method: "GET",
            headers: { "Accept": "text/html", "X-Soft-Nav": "1" },
            credentials: "same-origin",
            signal: currentController.signal
        })
            .then(function (response) {
                if (!response.ok) throw new Error("HTTP " + response.status);
                return response.text();
            })
            .then(function (html) {
                updateDocumentFrom(html, url);
                if (pushHistory) {
                    history.pushState({ softNav: true }, "", url);
                }
            })
            .catch(function (error) {
                if (error.name === "AbortError") return;
                window.location.href = url;
            })
            .finally(function () {
                currentController = null;
                setBusy(false);
            });
    }

    function clampDuration(value) {
        var parsed = parseInt(value, 10);
        if (isNaN(parsed)) parsed = 15;
        return Math.max(5, Math.min(180, parsed));
    }

    function durationInputFor(control) {
        var wrapper = control && control.closest ? control.closest("[data-duration-stepper]") : null;
        return wrapper ? wrapper.querySelector("[data-duration-input]") : null;
    }

    document.addEventListener("click", function (event) {
        var durationButton = event.target.closest ? event.target.closest("[data-duration-step]") : null;
        var link = event.target.closest ? event.target.closest("a[href]") : null;
        var input;
        var step;
        if (durationButton) {
            input = durationInputFor(durationButton);
            if (!input || input.disabled || input.readOnly) return;
            step = parseInt(durationButton.getAttribute("data-duration-step") || "0", 10) || 0;
            input.value = clampDuration((parseInt(input.value, 10) || 15) + step);
            input.dispatchEvent(new Event("change", { bubbles: true }));
            event.preventDefault();
            return;
        }
        if (!isSoftNavigableLink(link)) return;
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        navigate(link.href, true);
    });

    document.addEventListener("change", function (event) {
        var input = event.target;
        if (!input || !input.matches || !input.matches("[data-duration-input]")) return;
        if (input.disabled || input.readOnly) return;
        input.value = clampDuration(input.value);
    });

    document.addEventListener("submit", function (event) {
        var form = event.target;
        var method;
        var url;
        var params;
        if (!form || form.tagName !== "FORM" || form.hasAttribute("data-no-soft-nav")) return;
        method = (form.getAttribute("method") || "GET").toUpperCase();
        if (method !== "GET" || form.target) return;
        url = new URL(form.getAttribute("action") || window.location.href, window.location.href);
        if (!sameOrigin(url)) return;
        params = new URLSearchParams(new FormData(form));
        url.search = params.toString();
        event.preventDefault();
        navigate(url.href, true);
    });

    window.addEventListener("popstate", function () {
        navigate(window.location.href, false);
    });

    window.AppShell = {
        navigate: function (url) {
            navigate(new URL(url, window.location.href).href, true);
        },
        ready: function (callback) {
            if (document.readyState === "loading") {
                document.addEventListener("DOMContentLoaded", callback, { once: true });
            } else {
                callback();
            }
        }
    };
})();
