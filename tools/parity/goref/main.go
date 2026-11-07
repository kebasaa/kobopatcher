// Command goref applies kobopatch patch files with the Go implementation, for
// kobopatcher's parity harness (tools/parity/harness.py).
//
//	goref JOBS.jsonl RESULTS.jsonl
//
// Each job is {"id", "bin" | "tar"+"member", "patch", "format", "enable"}.
// If "enable" is present, exactly those patches are enabled; otherwise the
// file's own Enabled states are used.
package main

import (
	"archive/tar"
	"bufio"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"io/ioutil"
	"os"
	"path/filepath"

	"github.com/pgaskin/kobopatch/patchfile"
	"github.com/pgaskin/kobopatch/patchfile/kobopatch"
	_ "github.com/pgaskin/kobopatch/patchfile/patch32lsb"
	"github.com/pgaskin/kobopatch/patchlib"
	"github.com/xi2/xz"
)

type job struct {
	ID     string    `json:"id"`
	Bin    string    `json:"bin"`
	Tar    string    `json:"tar"`
	Member string    `json:"member"`
	Patch  string    `json:"patch"`
	Format string    `json:"format"`
	Enable *[]string `json:"enable"`
}

type result struct {
	ID          string   `json:"id"`
	Names       []string `json:"names,omitempty"`
	ParseErr    string   `json:"parse_err,omitempty"`
	ValidateErr string   `json:"validate_err,omitempty"`
	ApplyErr    string   `json:"apply_err,omitempty"`
	Panic       string   `json:"panic,omitempty"`
	SHA256      string   `json:"sha256,omitempty"`
	Size        int      `json:"size"`
}

var cacheKey string
var cacheBuf []byte

func load(j job) ([]byte, error) {
	key := j.Bin + "|" + j.Tar + "|" + j.Member
	if key == cacheKey {
		return cacheBuf, nil
	}
	var buf []byte
	if j.Bin != "" {
		b, err := ioutil.ReadFile(j.Bin)
		if err != nil {
			return nil, err
		}
		buf = b
	} else {
		f, err := os.Open(j.Tar)
		if err != nil {
			return nil, err
		}
		defer f.Close()
		xr, err := xz.NewReader(f, 0)
		if err != nil {
			return nil, err
		}
		tr := tar.NewReader(xr)
		for {
			h, err := tr.Next()
			if err == io.EOF {
				return nil, fmt.Errorf("member %s not found", j.Member)
			} else if err != nil {
				return nil, err
			}
			if h.Name == j.Member || filepath.Base(h.Name) == j.Member {
				buf, err = ioutil.ReadAll(tr)
				if err != nil {
					return nil, err
				}
				break
			}
		}
	}
	cacheKey, cacheBuf = key, buf
	return buf, nil
}

func run(j job) (r result) {
	r.ID = j.ID
	defer func() {
		if e := recover(); e != nil {
			r.Panic = fmt.Sprint(e)
		}
	}()
	orig, err := load(j)
	if err != nil {
		r.ParseErr = "load: " + err.Error()
		return
	}
	ps, err := patchfile.ReadFromFile(j.Format, j.Patch)
	if err != nil {
		r.ParseErr = err.Error()
		return
	}
	if kps, ok := ps.(*kobopatch.PatchSet); ok {
		r.Names = kps.SortedNames()
	}
	if j.Enable != nil {
		for _, n := range r.Names {
			ps.SetEnabled(n, false)
		}
		for _, n := range *j.Enable {
			if err := ps.SetEnabled(n, true); err != nil {
				r.ValidateErr = "enable: " + err.Error()
				return
			}
		}
	}
	if err := ps.Validate(); err != nil {
		r.ValidateErr = err.Error()
		return
	}
	buf := append([]byte(nil), orig...)
	pt := patchlib.NewPatcher(buf)
	pt.SetPath("./usr/local/Kobo/" + filepath.Base(j.Member+j.Bin))
	if err := ps.ApplyTo(pt); err != nil {
		r.ApplyErr = err.Error()
		return
	}
	out := pt.GetBytes()
	r.SHA256 = fmt.Sprintf("%x", sha256.Sum256(out))
	r.Size = len(out)
	return
}

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: goref JOBS.jsonl RESULTS.jsonl")
		os.Exit(2)
	}
	in, err := os.Open(os.Args[1])
	if err != nil {
		panic(err)
	}
	outf, err := os.Create(os.Args[2])
	if err != nil {
		panic(err)
	}
	w := bufio.NewWriter(outf)
	stdout := os.Stdout
	devnull, _ := os.Open(os.DevNull)
	os.Stdout = devnull // the formats print progress
	sc := bufio.NewScanner(in)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	n := 0
	for sc.Scan() {
		var j job
		if err := json.Unmarshal(sc.Bytes(), &j); err != nil {
			panic(err)
		}
		b, _ := json.Marshal(run(j))
		w.Write(append(b, '\n'))
		n++
	}
	w.Flush()
	outf.Close()
	fmt.Fprintf(stdout, "%d jobs\n", n)
}
