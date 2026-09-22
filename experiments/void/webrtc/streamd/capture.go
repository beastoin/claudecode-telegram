package streamd

import (
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"sync"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
)

// VideoBroadcaster receives RTP packets from ffmpeg and relays them to all WebRTC tracks.
type VideoBroadcaster struct {
	mu     sync.RWMutex
	tracks map[*webrtc.TrackLocalStaticRTP]struct{}
	conn   net.PacketConn
	done   chan struct{}
}

func NewVideoBroadcaster(listenAddr string) (*VideoBroadcaster, error) {
	conn, err := net.ListenPacket("udp", listenAddr)
	if err != nil {
		return nil, fmt.Errorf("listen RTP: %w", err)
	}
	vb := &VideoBroadcaster{
		tracks: make(map[*webrtc.TrackLocalStaticRTP]struct{}),
		conn:   conn,
		done:   make(chan struct{}),
	}
	go vb.readLoop()
	return vb, nil
}

func (vb *VideoBroadcaster) readLoop() {
	defer close(vb.done)
	buf := make([]byte, 1500)
	for {
		n, _, err := vb.conn.ReadFrom(buf)
		if err != nil {
			return
		}
		pkt := &rtp.Packet{}
		if err := pkt.Unmarshal(buf[:n]); err != nil {
			continue
		}
		vb.mu.RLock()
		for track := range vb.tracks {
			_ = track.WriteRTP(pkt)
		}
		vb.mu.RUnlock()
	}
}

func (vb *VideoBroadcaster) AddTrack(track *webrtc.TrackLocalStaticRTP) {
	vb.mu.Lock()
	vb.tracks[track] = struct{}{}
	vb.mu.Unlock()
}

func (vb *VideoBroadcaster) RemoveTrack(track *webrtc.TrackLocalStaticRTP) {
	vb.mu.Lock()
	delete(vb.tracks, track)
	vb.mu.Unlock()
}

func (vb *VideoBroadcaster) LocalAddr() string {
	return vb.conn.LocalAddr().String()
}

func (vb *VideoBroadcaster) Close() error {
	return vb.conn.Close()
}

// ScreenCapture manages Xvfb + Chromium + ffmpeg processes.
type ScreenCapture struct {
	display  string
	width    int
	height   int
	fps      int
	rtpAddr  string
	startURL string
	cdpPort  int

	xvfb     *exec.Cmd
	chromium *exec.Cmd
	ffmpeg   *exec.Cmd
}

func NewScreenCapture(display string, width, height, fps int, rtpAddr, startURL string, cdpPort int) *ScreenCapture {
	return &ScreenCapture{
		display:  display,
		width:    width,
		height:   height,
		fps:      fps,
		rtpAddr:  rtpAddr,
		startURL: startURL,
		cdpPort:  cdpPort,
	}
}

func (sc *ScreenCapture) Start() error {
	// Start Xvfb
	sc.xvfb = exec.Command("Xvfb", sc.display,
		"-screen", "0", fmt.Sprintf("%dx%dx24", sc.width, sc.height),
		"-ac", "-nolisten", "tcp",
	)
	sc.xvfb.Stdout = os.Stderr
	sc.xvfb.Stderr = os.Stderr
	if err := sc.xvfb.Start(); err != nil {
		return fmt.Errorf("start Xvfb: %w", err)
	}
	time.Sleep(500 * time.Millisecond)

	// Start Chromium
	sc.chromium = exec.Command("chromium",
		"--no-sandbox",
		"--disable-gpu",
		"--disable-software-rasterizer",
		"--disable-dev-shm-usage",
		fmt.Sprintf("--remote-debugging-port=%d", sc.cdpPort),
		"--remote-debugging-address=127.0.0.1",
		fmt.Sprintf("--window-size=%d,%d", sc.width, sc.height),
		"--kiosk",
		"--no-first-run",
		"--disable-translate",
		"--disable-infobars",
		sc.startURL,
	)
	sc.chromium.Env = append(os.Environ(), fmt.Sprintf("DISPLAY=%s", sc.display))
	sc.chromium.Stdout = os.Stderr
	sc.chromium.Stderr = os.Stderr
	if err := sc.chromium.Start(); err != nil {
		sc.Stop()
		return fmt.Errorf("start Chromium: %w", err)
	}
	time.Sleep(2 * time.Second)
	log.Printf("Chromium started on %s, CDP port %d", sc.display, sc.cdpPort)

	// Start ffmpeg: x11grab → H.264 → RTP
	sc.ffmpeg = exec.Command("ffmpeg",
		"-f", "x11grab",
		"-video_size", fmt.Sprintf("%dx%d", sc.width, sc.height),
		"-framerate", fmt.Sprintf("%d", sc.fps),
		"-i", sc.display,
		"-c:v", "libx264",
		"-preset", "ultrafast",
		"-tune", "zerolatency",
		"-profile:v", "baseline",
		"-level", "3.1",
		"-pix_fmt", "yuv420p",
		"-g", fmt.Sprintf("%d", sc.fps),
		"-b:v", "1M",
		"-maxrate", "1.5M",
		"-bufsize", "500k",
		"-an",
		"-payload_type", "96",
		"-ssrc", "1",
		"-f", "rtp", fmt.Sprintf("rtp://%s", sc.rtpAddr),
	)
	sc.ffmpeg.Env = append(os.Environ(), fmt.Sprintf("DISPLAY=%s", sc.display))
	sc.ffmpeg.Stderr = os.Stderr
	if err := sc.ffmpeg.Start(); err != nil {
		sc.Stop()
		return fmt.Errorf("start ffmpeg: %w", err)
	}

	log.Printf("Screen capture: %s %dx%d@%dfps → rtp://%s", sc.display, sc.width, sc.height, sc.fps, sc.rtpAddr)
	return nil
}

func (sc *ScreenCapture) Stop() {
	if sc.ffmpeg != nil && sc.ffmpeg.Process != nil {
		_ = sc.ffmpeg.Process.Kill()
		_ = sc.ffmpeg.Wait()
	}
	if sc.chromium != nil && sc.chromium.Process != nil {
		_ = sc.chromium.Process.Kill()
		_ = sc.chromium.Wait()
	}
	if sc.xvfb != nil && sc.xvfb.Process != nil {
		_ = sc.xvfb.Process.Kill()
		_ = sc.xvfb.Wait()
	}
}
