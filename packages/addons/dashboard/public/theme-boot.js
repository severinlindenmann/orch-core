/* Runs before first paint (a classic script in <head>; the CSP allows 'self' scripts, not inline ones).
   Reads the stored Theme choice (System / Light / Dark), sets data-theme (only for an explicit choice), the dark class
   and color-scheme on <html>. Same rules as src/lib/theme.ts. Nothing here may throw: storage can be blocked. */
(function () {
  var root = document.documentElement
  var choice = 'system'
  try {
    var v = localStorage.getItem('orch.theme')
    if (v === 'light' || v === 'dark') choice = v
  } catch (e) {}
  var dark = choice === 'dark'
  if (choice === 'system') {
    try { dark = !window.matchMedia('(prefers-color-scheme: light)').matches } catch (e) { dark = true }
    root.removeAttribute('data-theme')
  } else root.setAttribute('data-theme', choice)
  root.classList.toggle('dark', dark)
  root.style.colorScheme = dark ? 'dark' : 'light'
})()
