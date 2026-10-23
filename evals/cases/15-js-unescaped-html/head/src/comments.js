export function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function renderComment(el, comment) {
  el.innerHTML = "<p>" + comment.text + "</p>";
}
