package main

import (
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"void/streamd"
)

func main() {
	cfg, err := streamd.LoadConfigFromEnv()
	if err != nil {
		log.Fatalf("config error: %v", err)
	}

	// Screen capture settings
	display := envOr("DISPLAY_NUM", ":99")
	width := envInt("SCREEN_WIDTH", 720)
	height := envInt("SCREEN_HEIGHT", 1280)
	fps := envInt("SCREEN_FPS", 15)
	rtpAddr := envOr("RTP_ADDR", "127.0.0.1:5004")
	startURL := envOr("START_URL", "https://www.google.com")
	cdpPort := envInt("CDP_PORT", 9222)

	// Start screen capture (Xvfb + Chromium + ffmpeg)
	capture := streamd.NewScreenCapture(display, width, height, fps, rtpAddr, startURL, cdpPort)
	if err := capture.Start(); err != nil {
		log.Fatalf("screen capture: %v", err)
	}
	defer capture.Stop()

	// Wait for Chromium CDP to become available
	cdpURL := "http://127.0.0.1:" + strconv.Itoa(cdpPort)
	var cdp streamd.CDPClient
	for i := 0; i < 30; i++ {
		wsURL, err := streamd.DiscoverCDPWebSocket(cdpURL)
		if err == nil {
			cdpClient, err := streamd.NewCDPWSClient(wsURL)
			if err == nil {
				cdp = cdpClient
				log.Printf("CDP connected: %s", wsURL)
				break
			}
		}
		time.Sleep(500 * time.Millisecond)
	}
	if cdp == nil {
		log.Printf("WARNING: CDP not available, input dispatch disabled")
		cdp = &streamd.NoopCDPClient{}
	}

	// Start video broadcaster (receives RTP from ffmpeg, relays to WebRTC tracks)
	broadcaster, err := streamd.NewVideoBroadcaster(rtpAddr)
	if err != nil {
		log.Fatalf("video broadcaster: %v", err)
	}
	defer broadcaster.Close()

	// Create streamd server
	srv, err := streamd.NewServer(cfg, cdp, nil)
	if err != nil {
		log.Fatalf("server init: %v", err)
	}
	srv.SetBroadcaster(broadcaster)
	defer srv.Close()

	addr := envOr("STREAMD_ADDR", ":8097")
	log.Printf("void-streamd listening on %s", addr)

	// Graceful shutdown
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		log.Println("shutting down...")
		capture.Stop()
		srv.Close()
		broadcaster.Close()
		os.Exit(0)
	}()

	if err := http.ListenAndServe(addr, srv.Handler()); err != nil {
		log.Fatalf("listen: %v", err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		n, err := strconv.Atoi(v)
		if err == nil {
			return n
		}
	}
	return def
}
