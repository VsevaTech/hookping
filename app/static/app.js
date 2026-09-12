(function () {
  "use strict";

  // Copy-to-clipboard buttons.
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var text = button.getAttribute("data-copy");
      var done = function () {
        var original = button.textContent;
        button.textContent = "Copied";
        button.classList.add("copied");
        setTimeout(function () {
          button.textContent = original;
          button.classList.remove("copied");
        }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
      } else {
        fallbackCopy(text);
        done();
      }
    });
  });

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "absolute";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } catch (e) { /* ignore */ }
    document.body.removeChild(area);
  }

  // Insert a field placeholder into the template editor at the caret.
  var editor = document.getElementById("message_template");
  document.querySelectorAll("[data-insert]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      if (!editor) return;
      var snippet = chip.getAttribute("data-insert");
      var start = editor.selectionStart || 0;
      var end = editor.selectionEnd || 0;
      var value = editor.value;
      editor.value = value.slice(0, start) + snippet + value.slice(end);
      editor.focus();
      editor.selectionStart = editor.selectionEnd = start + snippet.length;
    });
  });

  // Confirmation prompts for destructive forms.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });

  // Poll Telegram connection status while a connection link is pending.
  var telegramCard = document.getElementById("telegram-card");
  if (telegramCard && telegramCard.getAttribute("data-connected") === "false" && telegramCard.querySelector("a[href^='https://t.me/']")) {
    var inboxId = telegramCard.getAttribute("data-inbox-id");
    var attempts = 0;
    var timer = setInterval(function () {
      attempts += 1;
      if (attempts > 300) { clearInterval(timer); return; } // stop after ~10 minutes
      fetch("/api/inboxes/" + inboxId + "/telegram/status", { headers: { Accept: "application/json" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (status) {
          if (status && status.connected) {
            clearInterval(timer);
            window.location.replace("/inboxes/" + inboxId + "?notice=" + encodeURIComponent("Telegram connected.") + "&kind=ok");
          }
        })
        .catch(function () { /* transient network error: keep polling */ });
    }, 2000);
  }
})();
