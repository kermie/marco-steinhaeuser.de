// Search over search-index.json (written by build.py). Loaded only on /search/.
(function () {
  "use strict";
  // Next to assets/; the site may live in a sub-folder.
  var indexUrl = new URL("../search-index.json", document.currentScript.src).href;
  var query = (new URLSearchParams(location.search).get("s") || "").trim();
  var input = document.getElementById("search-q");
  var status = document.getElementById("search-status");
  var list = document.getElementById("search-results");
  input.value = query;
  if (!query) { input.focus(); return; }

  // "Hoehe", "Höhe" and "hohe" all find "Höhe".
  function norm(s) {
    return s.toLowerCase().replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss");
  }
  var words = norm(query).split(/\s+/).filter(Boolean);

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  // Excerpt around the first hit, with the hits marked (built as DOM, never as HTML).
  function snippet(text) {
    var low = norm(text), at = -1;
    words.forEach(function (w) { var i = low.indexOf(w); if (i >= 0 && (at < 0 || i < at)) at = i; });
    var start = Math.max(0, at - 80);
    var part = (start ? "… " : "") + text.slice(start, start + 240) + (text.length > start + 240 ? " …" : "");
    var p = el("p");
    var re = new RegExp("(" + words.map(function (w) {
      return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/ae/g, "(?:ae|ä)").replace(/oe/g, "(?:oe|ö)")
              .replace(/ue/g, "(?:ue|ü)").replace(/ss/g, "(?:ss|ß)");
    }).join("|") + ")", "gi");
    part.split(re).forEach(function (piece, i) {
      p.appendChild(i % 2 ? el("mark", piece) : document.createTextNode(piece));
    });
    return p;
  }

  document.title = "Search results for “" + query + "” – " + document.title.split(" – ").pop();
  status.textContent = "Searching …";
  fetch(indexUrl).then(function (r) { return r.json(); }).then(function (docs) {
    var hits = [];
    docs.forEach(function (d) {
      var title = norm(d.t), body = norm(d.x), tags = norm(d.g || ""), score = 0;
      for (var i = 0; i < words.length; i++) {
        var inTitle = title.indexOf(words[i]) >= 0, inTags = tags.indexOf(words[i]) >= 0, inBody = body.indexOf(words[i]) >= 0;
        if (!inTitle && !inTags && !inBody) return;
        score += (inTitle ? 10 : 0) + (inTags ? 5 : 0) + (inBody ? 1 : 0);
      }
      hits.push({ doc: d, score: score });
    });
    hits.sort(function (a, b) { return b.score - a.score || (b.doc.s || "").localeCompare(a.doc.s || ""); });
    status.textContent = hits.length
      ? hits.length + (hits.length === 1 ? " result" : " results") + " for “" + query + "”"
      : "Nothing found for “" + query + "”. Try other words.";
    hits.slice(0, 50).forEach(function (h) {
      var li = el("li"), a = el("a", h.doc.t);
      a.href = new URL(h.doc.u, indexUrl).href;
      var head = el("h4"); head.appendChild(a); li.appendChild(head);
      if (h.doc.d) li.appendChild(el("div", h.doc.d, "bizberg_post_date"));
      li.appendChild(snippet(h.doc.x));
      list.appendChild(li);
    });
  }).catch(function () { status.textContent = "The search isn't available right now."; });
})();
