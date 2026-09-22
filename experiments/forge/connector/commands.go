package connector

import (
	"context"
	"fmt"
	"sort"
	"strings"
)

func RegisterBuiltinCommands(host *ConnectorHost, services CommandServices) {
	host.RegisterCommand(CommandSpec{
		Name:        "help",
		Description: "List available commands",
		Category:    "system",
	}, helpCommand(host))

	host.RegisterCommand(CommandSpec{
		Name:        "status",
		Description: "Show worker status",
		Category:    "system",
	}, statusCommand(services))

	if services.Auth != nil {
		host.RegisterCommand(CommandSpec{
			Name:        "login",
			Description: "Start OAuth login flow",
			Category:    "auth",
		}, loginCommand(services))

		host.RegisterCommand(CommandSpec{
			Name:        "logout",
			Description: "Clear credentials and log out",
			Category:    "auth",
		}, logoutCommand(services))
	}
}

type CommandServices struct {
	Runtime    Runtime
	Auth       AuthCommandService
	WorkerName string
}

type AuthCommandService interface {
	TriggerLogin(ctx context.Context) error
	Logout(ctx context.Context) error
	IsAuthenticated() bool
}

func helpCommand(host *ConnectorHost) CommandHandler {
	return func(ctx context.Context, inv CommandInvocation) CommandResult {
		cmds := host.Commands()
		sort.Slice(cmds, func(i, j int) bool { return cmds[i].Name < cmds[j].Name })

		var b strings.Builder
		b.WriteString("Available commands:\n")
		for _, cmd := range cmds {
			b.WriteString(fmt.Sprintf("  /%s — %s\n", cmd.Name, cmd.Description))
		}
		return CommandResult{Text: b.String()}
	}
}

func statusCommand(svc CommandServices) CommandHandler {
	return func(ctx context.Context, inv CommandInvocation) CommandResult {
		var b strings.Builder
		b.WriteString(fmt.Sprintf("Worker: %s\n", svc.WorkerName))

		if svc.Runtime != nil {
			if err := svc.Runtime.Health(); err != nil {
				b.WriteString("Runtime: unhealthy (" + err.Error() + ")\n")
			} else {
				b.WriteString("Runtime: healthy\n")
			}
		} else {
			b.WriteString("Runtime: not configured\n")
		}

		if svc.Auth != nil {
			if svc.Auth.IsAuthenticated() {
				b.WriteString("Auth: authenticated\n")
			} else {
				b.WriteString("Auth: not authenticated\n")
			}
		}

		return CommandResult{Text: b.String()}
	}
}

func loginCommand(svc CommandServices) CommandHandler {
	return func(ctx context.Context, inv CommandInvocation) CommandResult {
		if svc.Auth.IsAuthenticated() {
			return CommandResult{Text: "Already authenticated. Use /logout first to re-authenticate."}
		}
		if err := svc.Auth.TriggerLogin(ctx); err != nil {
			return CommandResult{Error: err}
		}
		return CommandResult{Text: "Login flow started. Check for auth prompt."}
	}
}

func logoutCommand(svc CommandServices) CommandHandler {
	return func(ctx context.Context, inv CommandInvocation) CommandResult {
		if err := svc.Auth.Logout(ctx); err != nil {
			return CommandResult{Error: err}
		}
		return CommandResult{Text: "Logged out. Credentials cleared."}
	}
}
