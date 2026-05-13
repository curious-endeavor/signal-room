// Copyright 2026 142. Licensed under Apache-2.0. See LICENSE.

package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// Pillars are named saved GDELT queries. They live in a flat JSON file
// (~/.config/gdelt-pp-cli/pillars.json) so they're easy to back up, edit by
// hand, and inspect. We deliberately don't put them in SQLite — the
// store is for cached *results*; this is small enough to be a config file.
type pillar struct {
	Name         string    `json:"name"`
	Query        string    `json:"query"`
	CreatedAt    time.Time `json:"created_at"`
	LastPulledAt time.Time `json:"last_pulled_at,omitempty"`
}

type pillarStore struct {
	Pillars []pillar `json:"pillars"`
}

func pillarsPath() (string, error) {
	if p := os.Getenv("GDELT_PILLARS_PATH"); p != "" {
		return p, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".config", "gdelt-pp-cli", "pillars.json"), nil
}

func loadPillars() (*pillarStore, string, error) {
	path, err := pillarsPath()
	if err != nil {
		return nil, "", err
	}
	store := &pillarStore{}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return store, path, nil
		}
		return nil, path, fmt.Errorf("reading %s: %w", path, err)
	}
	if err := json.Unmarshal(data, store); err != nil {
		return nil, path, fmt.Errorf("parsing %s: %w", path, err)
	}
	return store, path, nil
}

func savePillars(store *pillarStore, path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("creating pillars dir: %w", err)
	}
	data, err := json.MarshalIndent(store, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o600)
}

func (s *pillarStore) find(name string) (int, *pillar) {
	for i := range s.Pillars {
		if s.Pillars[i].Name == name {
			return i, &s.Pillars[i]
		}
	}
	return -1, nil
}

func newPillarCmd(flags *rootFlags) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "pillar",
		Short: "Manage named saved queries (pillars) for recurring topic monitoring",
		Long: `A pillar is a named, re-runnable GDELT query — like "child-safety-ai" mapped
to '("child safety" OR CSAM OR "age verification") AND (AI OR chatbot)'. Saving
queries as pillars means a monitoring workflow can refer to topics by stable
names instead of re-typing fragile boolean strings on every run.

Pillars are stored as JSON at ~/.config/gdelt-pp-cli/pillars.json (override with
$GDELT_PILLARS_PATH). Edit the file by hand if you want — the schema is
intentionally simple.`,
	}
	cmd.AddCommand(newPillarAddCmd(flags))
	cmd.AddCommand(newPillarListCmd(flags))
	cmd.AddCommand(newPillarRmCmd(flags))
	cmd.AddCommand(newPillarPullCmd(flags))
	return cmd
}

func newPillarAddCmd(flags *rootFlags) *cobra.Command {
	return &cobra.Command{
		Use:     "add <name> <query>",
		Short:   "Save a query under a name",
		Example: "  gdelt-pp-cli pillar add child-safety-ai '(\"child safety\" OR CSAM OR \"age verification\") AND (AI OR chatbot)'",
		Annotations: map[string]string{"mcp:read-only": "false"},
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) < 2 {
				return cmd.Help()
			}
			name := args[0]
			query := strings.Join(args[1:], " ")
			if dryRunOK(flags) {
				return nil
			}
			store, path, err := loadPillars()
			if err != nil {
				return err
			}
			if _, existing := store.find(name); existing != nil {
				// Overwriting in place is the right default — re-running `add`
				// with the same name is how the user updates a pillar query.
				existing.Query = query
			} else {
				store.Pillars = append(store.Pillars, pillar{
					Name:      name,
					Query:     query,
					CreatedAt: time.Now().UTC(),
				})
			}
			if err := savePillars(store, path); err != nil {
				return err
			}
			fmt.Fprintf(cmd.OutOrStdout(), "saved pillar %q\n", name)
			return nil
		},
	}
}

func newPillarListCmd(flags *rootFlags) *cobra.Command {
	return &cobra.Command{
		Use:     "list",
		Short:   "List saved pillars",
		Aliases: []string{"ls"},
		Annotations: map[string]string{"mcp:read-only": "true"},
		RunE: func(cmd *cobra.Command, args []string) error {
			store, _, err := loadPillars()
			if err != nil {
				return err
			}
			sort.Slice(store.Pillars, func(i, j int) bool { return store.Pillars[i].Name < store.Pillars[j].Name })
			if flags.asJSON || (!isTerminal(cmd.OutOrStdout()) && !flags.csv && !flags.quiet && !flags.plain) {
				body, _ := json.Marshal(store)
				return printOutputWithFlags(cmd.OutOrStdout(), body, flags)
			}
			if len(store.Pillars) == 0 {
				fmt.Fprintln(cmd.OutOrStdout(), "(no pillars saved — try: gdelt-pp-cli pillar add <name> <query>)")
				return nil
			}
			headers := []string{"NAME", "QUERY", "LAST PULLED"}
			rows := make([][]string, 0, len(store.Pillars))
			for _, p := range store.Pillars {
				last := "never"
				if !p.LastPulledAt.IsZero() {
					last = p.LastPulledAt.Format(time.RFC3339)
				}
				rows = append(rows, []string{p.Name, p.Query, last})
			}
			return flags.printTable(cmd, headers, rows)
		},
	}
}

func newPillarRmCmd(flags *rootFlags) *cobra.Command {
	return &cobra.Command{
		Use:     "rm <name>",
		Short:   "Remove a saved pillar",
		Aliases: []string{"remove", "delete"},
		Annotations: map[string]string{"mcp:read-only": "false"},
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 {
				return cmd.Help()
			}
			if dryRunOK(flags) {
				return nil
			}
			name := args[0]
			store, path, err := loadPillars()
			if err != nil {
				return err
			}
			i, existing := store.find(name)
			if existing == nil {
				return fmt.Errorf("no pillar named %q (run 'gdelt-pp-cli pillar list' to see saved pillars)", name)
			}
			store.Pillars = append(store.Pillars[:i], store.Pillars[i+1:]...)
			if err := savePillars(store, path); err != nil {
				return err
			}
			fmt.Fprintf(cmd.OutOrStdout(), "removed pillar %q\n", name)
			return nil
		},
	}
}

func newPillarPullCmd(flags *rootFlags) *cobra.Command {
	var (
		flagMax      int
		flagCountry  string
		flagLang     string
		flagTimespan string
		flagNoDedup  bool
	)
	cmd := &cobra.Command{
		Use:     "pull <name>",
		Short:   "Pull fresh hits for a saved pillar (like `today` but reusing the saved query)",
		Example: "  gdelt-pp-cli pillar pull child-safety-ai --json --max 50",
		Annotations: map[string]string{"mcp:read-only": "true"},
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 {
				return cmd.Help()
			}
			name := args[0]
			if dryRunOK(flags) {
				return nil
			}
			store, path, err := loadPillars()
			if err != nil {
				return err
			}
			_, p := store.find(name)
			if p == nil {
				return fmt.Errorf("no pillar named %q (run 'gdelt-pp-cli pillar list')", name)
			}

			params := map[string]string{
				"query":      p.Query,
				"mode":       "artlist",
				"format":     "json",
				"timespan":   flagTimespan,
				"sort":       "datedesc",
				"maxrecords": fmt.Sprintf("%d", flagMax),
			}
			if flagCountry != "" {
				params["sourcecountry"] = flagCountry
			}
			if flagLang != "" {
				params["sourcelang"] = flagLang
			}

			c, err := flags.newClient()
			if err != nil {
				return err
			}
			data, prov, err := resolveRead(cmd.Context(), c, flags, "pillar.pull", false, "/doc", params, nil)
			if err != nil {
				return classifyAPIError(err, flags)
			}
			data = extractResponseData(data)

			var raw struct {
				Articles []map[string]any `json:"articles"`
			}
			articles := []map[string]any{}
			if json.Unmarshal(data, &raw) == nil && len(raw.Articles) > 0 {
				articles = raw.Articles
			} else {
				_ = json.Unmarshal(data, &articles)
			}
			if !flagNoDedup {
				articles = dedupArticles(articles)
			}

			// Stamp the pull. Best-effort — a write failure here shouldn't
			// hide the article output the user actually asked for.
			p.LastPulledAt = time.Now().UTC()
			_ = savePillars(store, path)

			printProvenance(cmd, len(articles), prov)

			if flags.asJSON || (!isTerminal(cmd.OutOrStdout()) && !flags.csv && !flags.quiet && !flags.plain) {
				out := map[string]any{"pillar": p.Name, "query": p.Query, "articles": articles, "count": len(articles)}
				body, _ := json.Marshal(out)
				filtered := body
				if flags.selectFields != "" {
					filtered = filterFields(filtered, flags.selectFields)
				} else if flags.compact {
					filtered = compactFields(filtered)
				}
				wrapped, werr := wrapWithProvenance(filtered, prov)
				if werr != nil {
					return werr
				}
				return printOutput(cmd.OutOrStdout(), wrapped, true)
			}
			if wantsHumanTable(cmd.OutOrStdout(), flags) {
				if len(articles) == 0 {
					fmt.Fprintf(cmd.OutOrStdout(), "(no articles for pillar %q in the last %s)\n", p.Name, flagTimespan)
					return nil
				}
				if err := printAutoTable(cmd.OutOrStdout(), articles); err != nil {
					return err
				}
				return nil
			}
			body, _ := json.Marshal(articles)
			return printOutputWithFlags(cmd.OutOrStdout(), body, flags)
		},
	}
	cmd.Flags().IntVar(&flagMax, "max", 75, "Max articles to return (1-250)")
	cmd.Flags().StringVar(&flagCountry, "country", "", "Restrict to a source country (FIPS 2-letter code or name)")
	cmd.Flags().StringVar(&flagLang, "lang", "", "Restrict to a source language")
	cmd.Flags().StringVar(&flagTimespan, "timespan", "1d", "Relative window back from now (15min, 1h, 24h, 7d, 2w, 3m)")
	cmd.Flags().BoolVar(&flagNoDedup, "no-dedup", false, "Skip syndication dedup")
	return cmd
}
