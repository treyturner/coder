// SPDX-License-Identifier: MIT
package main

import (
	"archive/tar"
	"bytes"
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/registry"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/empty"
	"github.com/google/go-containerregistry/pkg/v1/layout"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/remote/transport"
	"github.com/google/go-containerregistry/pkg/v1/tarball"
	"github.com/google/go-containerregistry/pkg/v1/types"
)

func TestRepositoryFlags(t *testing.T) {
	t.Parallel()
	var repositories repositoriesFlag
	for _, value := range []string{"registry.test/owner/coder", "mirror.test/owner/coder"} {
		if err := repositories.Set(value); err != nil {
			t.Fatal("valid destination rejected")
		}
	}
	if len(repositories) != 2 {
		t.Fatal("destination flags were not retained")
	}
	for _, value := range []string{"registry.test/owner/coder", "https://registry.test/owner/coder", "user:fixture-secret@registry.test/owner/coder", "owner/coder", "registry.test/owner/coder:tag", "10.0.0.1/team/coder", "127.0.0.1/team/coder", "registry.local/team/coder"} {
		if err := repositories.Set(value); err == nil {
			t.Fatal("duplicate, authenticated, implicit, tagged or HTTP destination accepted")
		}
	}
}

func fixture(t *testing.T, binary string) *publication {
	t.Helper()
	var raw bytes.Buffer
	tw := tar.NewWriter(&raw)
	if err := tw.WriteHeader(&tar.Header{Name: "usr/local/bin/coder", Mode: 0o755, Size: int64(len(binary))}); err != nil {
		t.Fatal("fixture header")
	}
	if _, err := tw.Write([]byte(binary)); err != nil {
		t.Fatal("fixture body")
	}
	if err := tw.Close(); err != nil {
		t.Fatal("fixture tar")
	}
	layer, err := tarball.LayerFromReader(bytes.NewReader(raw.Bytes()), tarball.WithMediaType(types.OCILayer))
	if err != nil {
		t.Fatal("fixture compressed layer")
	}
	image, err := mutate.AppendLayers(empty.Image, layer)
	if err != nil {
		t.Fatal("fixture image")
	}
	image = mutate.MediaType(image, types.OCIManifestSchema1)
	config, err := image.ConfigFile()
	if err != nil {
		t.Fatal("fixture config")
	}
	config.OS = "linux"
	config.Architecture = "amd64"
	image, err = mutate.ConfigFile(image, config)
	if err != nil {
		t.Fatal("fixture platform config")
	}
	index := mutate.AppendManifests(empty.Index, mutate.IndexAddendum{Add: image, Descriptor: v1.Descriptor{Platform: &v1.Platform{OS: "linux", Architecture: "amd64"}}})
	path, err := layout.Write(t.TempDir(), empty.Index)
	if err != nil || path.AppendIndex(index) != nil {
		t.Fatal("fixture layout")
	}
	indexSum, err := index.Digest()
	if err != nil {
		t.Fatal("fixture index digest")
	}
	imageSum, err := image.Digest()
	if err != nil {
		t.Fatal("fixture image digest")
	}
	p, err := load(string(path), indexSum.String(), imageSum.String(), digest([]byte(binary)).Hex)
	if err != nil {
		t.Fatal("load synthetic layout:", err)
	}
	return p
}

func TestOnlyManifestHead404IsAbsent(t *testing.T) {
	t.Parallel()
	ref, err := name.NewTag("ghcr.io/treyturner/coder:2.37.4-proxyfix-1", name.StrictValidation)
	if err != nil {
		t.Fatal("fixture reference")
	}
	for _, test := range []struct {
		method string
		url    string
		absent bool
	}{
		{http.MethodHead, "https://ghcr.io/v2/treyturner/coder/manifests/2.37.4-proxyfix-1", true},
		{http.MethodGet, "https://ghcr.io/v2/treyturner/coder/manifests/2.37.4-proxyfix-1", false},
		{http.MethodGet, "https://ghcr.io/token", false},
		{http.MethodHead, "https://ghcr.io/v2/", false},
		{http.MethodHead, "https://elsewhere.invalid/v2/treyturner/coder/manifests/2.37.4-proxyfix-1", false},
	} {
		request, err := http.NewRequest(test.method, test.url, nil)
		if err != nil {
			t.Fatal("fixture request")
		}
		if absent(&transport.Error{StatusCode: http.StatusNotFound, Request: request}, ref) != test.absent {
			t.Fatal("non-manifest failure classified as tag absence")
		}
	}
}

type registryOptions struct {
	pathAuth     bool
	writeFailure *atomic.Int32
}

func localRegistry(t *testing.T, deny *atomic.Int32, writes *atomic.Int32, configuration ...registryOptions) (*httptest.Server, []name.Tag, []remote.Option) {
	t.Helper()
	var config registryOptions
	if len(configuration) != 0 {
		config = configuration[0]
	}
	backend := registry.New(registry.Logger(log.New(io.Discard, "", 0)))
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if config.pathAuth {
			user, password, ok := r.BasicAuth()
			if !ok || user != "synthetic-registry-user" || password != "synthetic-registry-password" {
				w.Header().Set("WWW-Authenticate", `Basic realm="fixture-registry"`)
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
		}
		if strings.HasPrefix(r.URL.Path, "/v2/second/") && deny.Load() != 0 {
			w.WriteHeader(int(deny.Load()))
			_, _ = w.Write([]byte("synthetic-remote-secret-marker"))
			return
		}
		if r.Method == http.MethodPut || r.Method == http.MethodPost || r.Method == http.MethodPatch {
			if config.writeFailure != nil && config.writeFailure.Load() != 0 && strings.HasPrefix(r.URL.Path, "/v2/second/") {
				w.WriteHeader(int(config.writeFailure.Load()))
				return
			}
			writes.Add(1)
		}
		backend.ServeHTTP(w, r)
	}))
	server.Config.ErrorLog = log.New(io.Discard, "", 0)
	t.Cleanup(server.Close)
	host := strings.TrimPrefix(server.URL, "https://")
	var refs []name.Tag
	for _, repository := range []string{"first", "second"} {
		ref, err := name.NewTag(host+"/"+repository+":2.37.4-proxyfix-1", name.StrictValidation)
		if err != nil {
			t.Fatal("fixture reference")
		}
		refs = append(refs, ref)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	t.Cleanup(cancel)
	return server, refs, []remote.Option{remote.WithContext(ctx), remote.WithTransport(server.Client().Transport), remote.WithAuth(authn.Anonymous)}
}

func TestPublicationAuthenticatedRoundtrip(t *testing.T) {
	t.Parallel()
	p := fixture(t, "synthetic authenticated CLI")
	var deny, writes atomic.Int32
	_, refs, opts := localRegistry(t, &deny, &writes, registryOptions{pathAuth: true})
	opts = append(opts, remote.WithAuth(authn.FromConfig(authn.AuthConfig{Username: "synthetic-registry-user", Password: "synthetic-registry-password"})))
	if err := p.publish(refs, opts, func(receipt) {}); err != nil {
		t.Fatal(err)
	}
}

func TestPublicationPartialFailureAndRetry(t *testing.T) {
	t.Parallel()
	p := fixture(t, "synthetic partial retry CLI")
	var deny, writes, failure atomic.Int32
	failure.Store(http.StatusForbidden)
	_, refs, opts := localRegistry(t, &deny, &writes, registryOptions{writeFailure: &failure})
	var events []receipt
	err := p.publish(refs, opts, func(r receipt) { events = append(events, r) })
	if err == nil || err.Error() != "registry-copy" || len(events) != 3 || events[1].Status != "verified" || events[2].Reference != refs[1].Name() {
		t.Fatal("partial publication receipt missing")
	}
	if err := p.verify(refs[0], opts); err != nil {
		t.Fatal("successful first publication was lost")
	}
	failure.Store(0)
	if err := p.publish(refs, opts, func(receipt) {}); err != nil {
		t.Fatal("same-digest retry did not recover")
	}
}

func TestPublicationRoundtrip(t *testing.T) {
	t.Parallel()
	p := fixture(t, "synthetic CLI fixture")
	var deny, writes atomic.Int32
	_, refs, opts := localRegistry(t, &deny, &writes)
	var events []receipt
	if err := p.publish(refs, opts, func(r receipt) { events = append(events, r) }); err != nil {
		t.Fatal(err)
	}
	if len(events) != 4 || events[1].Status != "verified" || events[3].Status != "verified" {
		t.Fatal("missing registry receipts")
	}
	before := writes.Load()
	if before == 0 {
		t.Fatal("fixture did not exercise registry uploads")
	}
	if err := p.publish(refs, opts, func(receipt) {}); err != nil || writes.Load() != before {
		t.Fatal("same-digest retry wrote registry state")
	}
}

func TestPublicationConflictBeforeWrites(t *testing.T) {
	t.Parallel()
	p := fixture(t, "synthetic expected CLI")
	other := fixture(t, "synthetic conflicting CLI")
	var deny, writes atomic.Int32
	_, refs, opts := localRegistry(t, &deny, &writes)
	if err := remote.WriteIndex(refs[1], other.index, opts...); err != nil {
		t.Fatal("seed conflicting tag")
	}
	before := writes.Load()
	err := p.publish(refs, opts, func(receipt) {})
	if err == nil || err.Error() != "tag-conflict" || writes.Load() != before {
		t.Fatal("conflicting second registry did not stop before first write")
	}
}

func TestPublicationDenialsBeforeWrites(t *testing.T) {
	t.Parallel()
	for _, status := range []int{http.StatusUnauthorized, http.StatusForbidden, http.StatusInternalServerError} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			t.Parallel()
			p := fixture(t, "synthetic expected CLI")
			var deny, writes atomic.Int32
			deny.Store(int32(status))
			_, refs, opts := localRegistry(t, &deny, &writes)
			err := p.publish(refs, opts, func(receipt) {})
			if err == nil || err.Error() != "preflight-access" || writes.Load() != 0 || strings.Contains(err.Error(), "synthetic-remote-secret-marker") {
				t.Fatal("denial did not fail closed with a safe diagnostic")
			}
		})
	}
}

func TestPublicationTLSAndCancellation(t *testing.T) {
	t.Parallel()
	p := fixture(t, "synthetic expected CLI")
	var deny, writes atomic.Int32
	_, refs, _ := localRegistry(t, &deny, &writes)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	// The process system roots cannot trust this independently generated CA.
	opts := []remote.Option{remote.WithContext(ctx), remote.WithTransport(http.DefaultTransport.(*http.Transport).Clone()), remote.WithAuth(authn.Anonymous)}
	if err := p.publish(refs, opts, func(receipt) {}); err == nil || writes.Load() != 0 {
		t.Fatal("untrusted TLS proceeded to upload")
	}
	canceled, stop := context.WithCancel(context.Background())
	stop()
	opts = []remote.Option{remote.WithContext(canceled), remote.WithAuth(authn.Anonymous)}
	if err := p.publish(refs, opts, func(receipt) {}); err == nil || writes.Load() != 0 {
		t.Fatal("canceled publication proceeded to upload")
	}
}

func TestPublicationReadbackRejectsWrongCLI(t *testing.T) {
	t.Parallel()
	p := fixture(t, "synthetic expected CLI")
	var deny, writes atomic.Int32
	_, refs, opts := localRegistry(t, &deny, &writes)
	if err := remote.WriteIndex(refs[0], p.index, opts...); err != nil {
		t.Fatal("seed tag")
	}
	p.binary = digest([]byte("synthetic different CLI")).Hex
	if err := p.verify(refs[0], opts); err == nil || err.Error() != "readback-cli-digest" {
		t.Fatal("wrong CLI checksum accepted")
	}
}
