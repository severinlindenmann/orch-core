// Loads the code of the addon node renderers that come in their own chunks (widgets, forms, markdown, charts,
// terminals), so a page that shows them renders them at once instead of a placeholder of another size. Route
// loaders call it for the pages that show addon content (addon pages, a ticket's panels, an addon's settings).
export { preloadAddonNodes } from './AddonNode'
