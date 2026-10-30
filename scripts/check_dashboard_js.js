// Extract the inline script from dashboard.html and syntax-check it with new Function().
const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "dashboard.html"), "utf8");
const start = html.indexOf("<script>");
const end = html.lastIndexOf("</" + "script>");
if (start < 0 || end < 0) {
  console.error("no inline script block found");
  process.exit(1);
}
const js = html.slice(start + 8, end);
try {
  new Function(js);
  console.log("dashboard JS OK");
} catch (e) {
  console.error("SYNTAX ERROR:", e.message);
  process.exit(1);
}
