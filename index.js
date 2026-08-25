/**
 * Runtime marker for dshmarket validation.
 *
 * This package is a configuration-only dsh bundle; its actual integration is
 * declared in cordis.patch.yml (which registers ywleeo/browser-mcp as a native
 * dsh MCP client). The side-effect-free ESM entry makes that contract explicit
 * for package consumers and marketplace validation.
 */
export {}
