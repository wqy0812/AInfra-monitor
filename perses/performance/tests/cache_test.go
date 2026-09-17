package ui

import (
	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
	"net/http"
	"net/http/httptest"
	"testing"
	"testing/fstest"
)

func TestPerformanceCache(t *testing.T) {
	old := asts
	defer func() { asts = old }()
	asts = http.FS(fstest.MapFS{"app/dist/main.12345678.js": {Data: []byte("PREFIX_PATH_PLACEHOLDER/main")}, "app/dist/index.html": {Data: []byte("<html>PREFIX_PATH_PLACEHOLDER</html>")}})
	request := func(prefix, path, etag string) *httptest.ResponseRecorder {
		e := echo.New()
		q := httptest.NewRequest("GET", path, nil)
		q.Header.Set("If-None-Match", etag)
		w := httptest.NewRecorder()
		require.NoError(t, (&frontend{apiPrefix: prefix}).assetHandler()(e.NewContext(q, w)))
		return w
	}
	a := request("/one", "/app/dist/main.12345678.js", "")
	require.Contains(t, a.Header().Get("Cache-Control"), "immutable")
	require.Equal(t, "/one/main", a.Body.String())
	b := request("/one", "/app/dist/main.12345678.js", a.Header().Get("ETag"))
	require.Equal(t, 304, b.Code)
	require.Empty(t, b.Body.String())
	c := request("/two", "/app/dist/main.12345678.js", a.Header().Get("ETag"))
	require.Equal(t, 200, c.Code)
	require.NotEqual(t, a.Header().Get("ETag"), c.Header().Get("ETag"))
	d := request("", "/app/dist/index.html", "")
	require.Equal(t, "no-cache", d.Header().Get("Cache-Control"))
}
