// SPDX-License-Identifier: MIT
package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/layout"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/remote/transport"
	"github.com/google/go-containerregistry/pkg/v1/types"
	"github.com/google/go-containerregistry/pkg/v1/validate"
)

type publication struct {
	index    v1.ImageIndex
	indexRaw []byte
	imageRaw []byte
	manifest *v1.Manifest
	indexSum v1.Hash
	imageSum v1.Hash
	binary   string
}

type receipt struct {
	Reference string `json:"reference"`
	Status    string `json:"status"`
}

func digest(data []byte) v1.Hash {
	h := sha256.Sum256(data)
	return v1.Hash{Algorithm: "sha256", Hex: hex.EncodeToString(h[:])}
}

func load(path, indexExpected, imageExpected, binaryExpected string) (*publication, error) {
	p, err := layout.FromPath(path)
	if err != nil {
		return nil, errors.New("read-layout")
	}
	catalog, err := p.ImageIndex()
	if err != nil {
		return nil, errors.New("read-catalog")
	}
	m, err := catalog.IndexManifest()
	if err != nil || len(m.Manifests) != 1 || m.Manifests[0].Digest.String() != indexExpected {
		return nil, errors.New("catalog-identity")
	}
	index, err := catalog.ImageIndex(m.Manifests[0].Digest)
	if err != nil || validate.Index(index) != nil {
		return nil, errors.New("validate-index")
	}
	raw, err := index.RawManifest()
	if err != nil || digest(raw).String() != indexExpected {
		return nil, errors.New("index-identity")
	}
	im, err := index.IndexManifest()
	if err != nil || len(im.Manifests) != 1 || im.Manifests[0].Digest.String() != imageExpected || im.Manifests[0].MediaType != types.OCIManifestSchema1 {
		return nil, errors.New("platform-identity")
	}
	image, err := index.Image(im.Manifests[0].Digest)
	if err != nil {
		return nil, errors.New("read-image")
	}
	imageRaw, err := image.RawManifest()
	if err != nil || digest(imageRaw).String() != imageExpected {
		return nil, errors.New("manifest-identity")
	}
	manifest, err := image.Manifest()
	if err != nil || len(manifest.Layers) == 0 {
		return nil, errors.New("read-manifest")
	}
	last := manifest.Layers[len(manifest.Layers)-1]
	layer, err := image.LayerByDigest(last.Digest)
	if err != nil {
		return nil, errors.New("local-layer")
	}
	reader, err := layer.Compressed()
	if err != nil {
		return nil, errors.New("local-blob")
	}
	data, err := io.ReadAll(io.LimitReader(reader, last.Size+1))
	closeErr := reader.Close()
	if err != nil || closeErr != nil || int64(len(data)) != last.Size || digest(data) != last.Digest || verifyBinary(data, binaryExpected) != nil {
		return nil, errors.New("local-cli-validation")
	}
	return &publication{index, raw, imageRaw, manifest, digest(raw), digest(imageRaw), binaryExpected}, nil
}

// absent distinguishes a genuine HTTP 404 from auth, TLS and network failures.
func absent(err error, ref name.Tag) bool {
	var status *transport.Error
	if !errors.As(err, &status) || status.StatusCode != http.StatusNotFound || status.Request == nil {
		return false
	}
	r := status.Request
	return r.Method == http.MethodHead && r.URL != nil && r.URL.Scheme == "https" && r.URL.Host == ref.RegistryStr() && r.URL.Path == "/v2/"+ref.RepositoryStr()+"/manifests/"+ref.TagStr()
}

func (p *publication) preflight(ref name.Tag, opts []remote.Option) (bool, error) {
	d, err := remote.Head(ref, opts...)
	if err != nil {
		if absent(err, ref) {
			return false, nil
		}
		return false, errors.New("preflight-access")
	}
	if d.Digest != p.indexSum {
		return false, errors.New("tag-conflict")
	}
	return true, nil
}

func (p *publication) verify(ref name.Tag, opts []remote.Option) error {
	d, err := remote.Get(ref, opts...)
	if err != nil || d.Digest != p.indexSum || !bytes.Equal(d.Manifest, p.indexRaw) {
		return errors.New("readback-index")
	}
	child, err := remote.Get(ref.Context().Digest(p.imageSum.String()), opts...)
	if err != nil || child.Digest != p.imageSum || !bytes.Equal(child.Manifest, p.imageRaw) {
		return errors.New("readback-manifest")
	}
	image, err := child.Image()
	if err != nil {
		return errors.New("readback-image")
	}
	config, err := image.RawConfigFile()
	if err != nil || digest(config) != p.manifest.Config.Digest {
		return errors.New("readback-config")
	}
	for i, desc := range p.manifest.Layers {
		layer, err := image.LayerByDigest(desc.Digest)
		if err != nil {
			return errors.New("readback-layer")
		}
		r, err := layer.Compressed()
		if err != nil {
			return errors.New("readback-blob")
		}
		data, err := io.ReadAll(io.LimitReader(r, desc.Size+1))
		closeErr := r.Close()
		if err != nil || closeErr != nil || int64(len(data)) != desc.Size || digest(data) != desc.Digest {
			return errors.New("readback-blob-digest")
		}
		if i == len(p.manifest.Layers)-1 {
			if err := verifyBinary(data, p.binary); err != nil {
				return err
			}
		}
	}
	return nil
}

func verifyBinary(compressed []byte, expected string) error {
	r, err := gzip.NewReader(bytes.NewReader(compressed))
	if err != nil {
		return errors.New("readback-layer-compression")
	}
	defer func() { _ = r.Close() }()
	tarReader := tar.NewReader(r)
	found := 0
	for {
		header, err := tarReader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return errors.New("readback-layer-tar")
		}
		if header.Name != "usr/local/bin/coder" {
			continue
		}
		if header.Typeflag != tar.TypeReg {
			return errors.New("readback-cli-type")
		}
		found++
		h := sha256.New()
		if _, err := io.Copy(h, tarReader); err != nil || hex.EncodeToString(h.Sum(nil)) != expected {
			return errors.New("readback-cli-digest")
		}
	}
	if found != 1 {
		return errors.New("readback-cli-count")
	}
	return nil
}

func (p *publication) publish(refs []name.Tag, opts []remote.Option, emit func(receipt)) error {
	existing := make([]bool, len(refs))
	// Preflight every destination before the first write to either registry.
	for i, ref := range refs {
		var err error
		existing[i], err = p.preflight(ref, opts)
		if err != nil {
			emit(receipt{ref.Name(), "preflight-failed"})
			return err
		}
	}
	for i, ref := range refs {
		emit(receipt{ref.Name(), "started"})
		if !existing[i] {
			// Repeat preflight just before writing to reduce external-writer races.
			same, err := p.preflight(ref, opts)
			if err != nil {
				return err
			}
			if !same {
				if err := remote.WriteIndex(ref, p.index, opts...); err != nil {
					return errors.New("registry-copy")
				}
			}
		}
		if err := p.verify(ref, opts); err != nil {
			return err
		}
		emit(receipt{ref.Name(), "verified"})
	}
	return nil
}

type repositoriesFlag []string

func (r *repositoriesFlag) String() string { return strings.Join(*r, ",") }

func (r *repositoriesFlag) Set(value string) error {
	repository, err := name.NewRepository(value, name.StrictValidation)
	if err != nil || repository.Name() != value || repository.Registry.Scheme() != "https" || !regexp.MustCompile(`^[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9._/-]*$`).MatchString(value) {
		return errors.New("invalid destination repository")
	}
	for _, existing := range *r {
		if existing == value {
			return errors.New("duplicate destination repository")
		}
	}
	*r = append(*r, value)
	return nil
}

func main() {
	var repositories repositoriesFlag
	flag.Var(&repositories, "repository", "approved destination repository; repeat for each registry")
	oci := flag.String("oci", "", "verified OCI layout")
	index := flag.String("index", "", "approved registry index digest")
	manifest := flag.String("manifest", "", "approved platform manifest digest")
	binary := flag.String("binary", "", "approved CLI checksum")
	tag := flag.String("tag", "", "versioned destination tag")
	verifyLocal := flag.Bool("verify-local", false, "verify local payload without registry access")
	flag.Parse()
	fail := func(stage string) {
		_ = json.NewEncoder(os.Stdout).Encode(map[string]string{"status": "failed", "stage": stage})
		os.Exit(1)
	}
	if !regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?-proxyfix-[1-9][0-9]*$`).MatchString(*tag) || !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(*binary) {
		fail("destination-tag")
	}
	p, err := load(*oci, *index, *manifest, *binary)
	if err != nil {
		fail(err.Error())
	}
	if *verifyLocal {
		_ = json.NewEncoder(os.Stdout).Encode(map[string]string{"status": "local-verified", "index": p.indexSum.String(), "manifest": p.imageSum.String()})
		return
	}
	if len(repositories) == 0 {
		fail("destination-repositories")
	}
	var refs []name.Tag
	for _, repository := range repositories {
		ref, err := name.NewTag(repository+":"+*tag, name.StrictValidation)
		if err != nil {
			fail("destination-reference")
		}
		refs = append(refs, ref)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	opts := []remote.Option{remote.WithContext(ctx), remote.WithAuthFromKeychain(authn.DefaultKeychain)}
	if err := p.publish(refs, opts, func(r receipt) { _ = json.NewEncoder(os.Stdout).Encode(r) }); err != nil {
		fail(err.Error())
	}
}
