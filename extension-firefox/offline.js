// Store the original .onion URL (passed via query param)
const params = new URLSearchParams(window.location.search);
const originalUrl = params.get("url");

document.getElementById("retry-btn").addEventListener("click", (e) => {
  e.preventDefault();
  if (originalUrl) {
    window.location.href = originalUrl;
  } else {
    window.location.reload();
  }
});
