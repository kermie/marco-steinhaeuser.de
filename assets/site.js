// Small fixes on top of the Bizberg theme script (custom.js).
jQuery(document).on("click", ".full-screen-search .close", function (e) {
  e.preventDefault(); // the link is "#": don't jump to the top
});

// Old WordPress search addresses (/?s=word) -> the search page. nginx
// ignores .htaccess, so this happens here.
(function () {
  var q = new URLSearchParams(location.search).get("s");
  var body = document.body.classList;
  if (q !== null && body.contains("home") && !body.contains("paged")) {
    location.replace("search/?s=" + encodeURIComponent(q)); // relative to the homepage
  }
})();
