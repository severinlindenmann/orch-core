/**
 * The first screen: the same markup as the `.boot` block in index.html (classes from /loading.css), so the screen does not
 * change when React takes over from the HTML and while the shell data or the feature list is still loading. Never blank.
 */
export function BootScreen() {
  return (
    <div className="boot" role="status">
      <img src="/favicon.svg" alt="" />
      <b>orch</b>
      <span>Loading…</span>
    </div>
  )
}
