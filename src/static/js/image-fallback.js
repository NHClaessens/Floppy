// An image that fails to load renders as the browser's broken-image glyph,
// which reads as a bug rather than as missing artwork. Templates only fall back
// to IMG_NONE when the *stored* value is blank, so a poster whose URL is fine
// but whose fetch fails - a provider image-cache miss, or an Audiobookshelf
// server that is offline (#861) - had nothing to catch it.
(function () {
  var placeholder = document.documentElement.dataset.imgNone;
  if (!placeholder) return;

  // Image load errors do not bubble, so this has to listen in the capture phase
  // to see them at the document level.
  document.addEventListener(
    "error",
    function (event) {
      var img = event.target;
      if (!img || img.tagName !== "IMG") return;
      // Guard against a placeholder that itself fails, which would loop.
      if (img.dataset.imageFallbackApplied) return;
      if (img.getAttribute("src") === placeholder) return;
      img.dataset.imageFallbackApplied = "1";
      img.src = placeholder;
    },
    true,
  );

  // The listener is attached after parsing, so an eagerly-loaded image that
  // already failed never fires an event this can see. A broken image reports
  // complete with a zero natural width, which is what finds those.
  function sweep() {
    document.querySelectorAll("img").forEach(function (img) {
      if (img.dataset.imageFallbackApplied) return;
      if (!img.complete || img.naturalWidth !== 0) return;
      if (img.getAttribute("src") === placeholder) return;
      if (!img.getAttribute("src")) return;
      img.dataset.imageFallbackApplied = "1";
      img.src = placeholder;
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", sweep);
  } else {
    sweep();
  }
})();
