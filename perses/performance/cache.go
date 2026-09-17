package ui

import (
	"crypto/sha256"
	"fmt"
	"net/http"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/labstack/echo/v4"
)

var fingerprintedAsset = regexp.MustCompile(`(?i)(?:^|[./])[a-z0-9_-]*[.]?[a-f0-9]{8,}[.](?:js|css|woff2?|ttf|eot|png|svg|ico)$`)

// Validators describe the bytes after URL prefix substitution. Only content-
// addressed assets get immutable caching; manifests and HTML revalidate.
func writeCachedAsset(c echo.Context, name string, data []byte) error {
	h := c.Response().Header()
	h.Set("Cache-Control", "no-cache")
	if fingerprintedAsset.MatchString(filepath.Base(name)) {
		h.Set("Cache-Control", "public, max-age=31536000, immutable")
	}
	etag := fmt.Sprintf(`W/"%x"`, sha256.Sum256(data))
	h.Set("ETag", etag)
	for _, candidate := range strings.Split(c.Request().Header.Get("If-None-Match"), ",") {
		candidate = strings.TrimSpace(candidate)
		if candidate == "*" || strings.TrimPrefix(candidate, "W/") == strings.TrimPrefix(etag, "W/") {
			return c.NoContent(http.StatusNotModified)
		}
	}
	_, err := c.Response().Write(data)
	return err
}
