// Small fixes on top of the Bizberg theme script (custom.js).
jQuery(document).on("click", ".full-screen-search .close", function (e) {
  e.preventDefault(); // the link is "#": don't jump to the top
});
