// Copyright 2026 142. Licensed under Apache-2.0. See LICENSE.

package cli

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"sort"
	"strings"

	"github.com/spf13/cobra"
)

// newTodayCmd builds the headline transcendence command: "what's the world
// reporting on this topic in the last 24h", deduped and newest-first.
//
// It's a thin convenience wrapper over the GDELT DOC 2.0 artlist endpoint
// with opinionated defaults — timespan=1d, sort=datedesc — plus syndication
// dedup that no upstream tool does. The output is the same article shape
// `news` returns so downstream agents can ingest either.
func newTodayCmd(flags *rootFlags) *cobra.Command {
	var (
		flagMax           int
		flagCountry       string
		flagLang          string
		flagDomain        string
		flagExcludeDomain string
		flagTheme         string
		flagTimespan      string
		flagNoDedup       bool
		flagPrintQuery    bool
	)

	cmd := &cobra.Command{
		Use:   "today <topic>",
		Short: "News on a topic today from around the world (last 24h, deduped, newest first)",
		Long: `Pull worldwide news on a topic from the last 24 hours.

Wraps the GDELT DOC 2.0 artlist endpoint with the right defaults for "what
is the world saying about X right now" — a 24-hour window, results sorted
newest first, and syndicated near-duplicates collapsed so 250 rows of one
wire story become one row with an "also_in" list.

The <topic> argument is passed through to GDELT as the query, so its full
operator vocabulary works: phrases ("child safety"), booleans ((CSAM OR
"age verification") AND (AI OR chatbot)), and negation (-domain:reddit.com).`,
		Example: strings.Trim(`
  gdelt-pp-cli today "child safety AI"
  gdelt-pp-cli today "ai chatbot regulation" --json --max 25
  gdelt-pp-cli today '("character.ai" OR "Replika") AND teen' --country US --json
`, "\n"),
		Annotations: map[string]string{"mcp:read-only": "true"},
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 {
				return cmd.Help()
			}
			topic := strings.Join(args, " ")
			if dryRunOK(flags) {
				return nil
			}

			params := map[string]string{
				"query":      topic,
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
			// Domain / exclude-domain are folded directly into the query using
			// GDELT's `domain:` and negation syntax. This matches what users
			// would type if they composed the query themselves and avoids a
			// second URL parameter that the API doesn't have.
			if flagDomain != "" {
				params["query"] = params["query"] + " domain:" + flagDomain
			}
			if flagExcludeDomain != "" {
				params["query"] = params["query"] + " -domain:" + flagExcludeDomain
			}
			if flagTheme != "" {
				params["query"] = params["query"] + " theme:" + flagTheme
			}

			if flagPrintQuery {
				vals := url.Values{}
				for k, v := range params {
					vals.Set(k, v)
				}
				fmt.Fprintln(cmd.OutOrStdout(), "/doc?"+vals.Encode())
				return nil
			}

			c, err := flags.newClient()
			if err != nil {
				return err
			}
			data, prov, err := resolveRead(cmd.Context(), c, flags, "today", false, "/doc", params, nil)
			if err != nil {
				return classifyAPIError(err, flags)
			}
			data = extractResponseData(data)

			// Unwrap into a slice of article maps so we can dedup.
			var raw struct {
				Articles []map[string]any `json:"articles"`
			}
			articles := []map[string]any{}
			if json.Unmarshal(data, &raw) == nil && len(raw.Articles) > 0 {
				articles = raw.Articles
			} else {
				// Some responses come back as a bare array; tolerate both.
				_ = json.Unmarshal(data, &articles)
			}

			if !flagNoDedup {
				articles = dedupArticles(articles)
			}

			// Print provenance to stderr (count after dedup).
			printProvenance(cmd, len(articles), prov)

			// JSON / piped output gets the structured shape. Human-mode prints
			// a table. Both honor --select / --compact via the standard helpers.
			if flags.asJSON || (!isTerminal(cmd.OutOrStdout()) && !flags.csv && !flags.quiet && !flags.plain) {
				out := map[string]any{"articles": articles, "count": len(articles)}
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
					fmt.Fprintln(cmd.OutOrStdout(), "(no articles found in the last "+flagTimespan+")")
					return nil
				}
				if err := printAutoTable(cmd.OutOrStdout(), articles); err != nil {
					return err
				}
				if len(articles) >= 25 {
					fmt.Fprintf(os.Stderr, "\nShowing %d results. To narrow: add --max, --country, or --json --select.\n", len(articles))
				}
				return nil
			}
			body, _ := json.Marshal(articles)
			return printOutputWithFlags(cmd.OutOrStdout(), body, flags)
		},
	}
	cmd.Flags().IntVar(&flagMax, "max", 75, "Max articles to return (1-250)")
	cmd.Flags().StringVar(&flagCountry, "country", "", "Restrict to a source country (FIPS 2-letter code or name, e.g. US, UK, france)")
	cmd.Flags().StringVar(&flagLang, "lang", "", "Restrict to a source language (e.g. english, spanish, or 3-letter code)")
	cmd.Flags().StringVar(&flagDomain, "domain", "", "Restrict to this domain (folded into the query as domain:<value>)")
	cmd.Flags().StringVar(&flagExcludeDomain, "exclude-domain", "", "Exclude this domain (folded into the query as -domain:<value>)")
	cmd.Flags().StringVar(&flagTheme, "theme", "", "Restrict to a GKG theme (e.g. TERROR, ECON_*; folded into the query as theme:<value>)")
	cmd.Flags().StringVar(&flagTimespan, "timespan", "1d", "Override the 24h window (15min, 1h, 24h, 7d, 2w; max ~3 months)")
	cmd.Flags().BoolVar(&flagNoDedup, "no-dedup", false, "Skip syndication dedup (return every article, including near-duplicates)")
	cmd.Flags().BoolVar(&flagPrintQuery, "print-query", false, "Print the compiled GDELT query and exit (debugging)")
	return cmd
}

// dedupArticles collapses near-duplicate articles that wire-services produce.
// Heuristic: the dedup key is the lowercased title with all non-alphanumeric
// characters stripped. Same wire story copied to 30 outlets shares the title
// after the byline. The earliest seendate wins; the dropped articles' domains
// are surfaced as an `also_in` array on the kept row so the agent doesn't lose
// the reach signal.
//
// This is deliberately a title-only heuristic. Some outlets retitle aggressively
// (e.g. localized SEO titles); --no-dedup is the escape hatch for those.
func dedupArticles(in []map[string]any) []map[string]any {
	type group struct {
		kept    map[string]any
		domains []string
		count   int
	}
	keyOf := func(title string) string {
		var b strings.Builder
		for _, r := range strings.ToLower(title) {
			if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
				b.WriteRune(r)
			}
		}
		return b.String()
	}
	groups := map[string]*group{}
	order := []string{}
	for _, a := range in {
		title, _ := a["title"].(string)
		domain, _ := a["domain"].(string)
		if title == "" {
			// No title — pass through as its own group keyed by URL.
			url, _ := a["url"].(string)
			key := "url:" + url
			groups[key] = &group{kept: a, count: 1}
			order = append(order, key)
			continue
		}
		key := keyOf(title)
		if key == "" {
			key = "title:" + title
		}
		g, ok := groups[key]
		if !ok {
			g = &group{kept: a, count: 1}
			groups[key] = g
			order = append(order, key)
			continue
		}
		g.count++
		// Track the merged-in domains so the user can see total reach.
		if domain != "" && domain != domainOf(g.kept) {
			seen := false
			for _, d := range g.domains {
				if d == domain {
					seen = true
					break
				}
			}
			if !seen {
				g.domains = append(g.domains, domain)
			}
		}
		// Prefer the earliest seendate as the canonical row.
		if cmpSeenDate(a, g.kept) < 0 {
			oldDomains := g.domains
			g.kept = a
			g.domains = oldDomains
		}
	}
	out := make([]map[string]any, 0, len(order))
	for _, key := range order {
		g := groups[key]
		row := g.kept
		if len(g.domains) > 0 {
			// Sort for stable output.
			sort.Strings(g.domains)
			row["also_in"] = g.domains
		}
		if g.count > 1 {
			row["copies"] = g.count
		}
		out = append(out, row)
	}
	return out
}

func domainOf(a map[string]any) string {
	if a == nil {
		return ""
	}
	d, _ := a["domain"].(string)
	return d
}

func cmpSeenDate(a, b map[string]any) int {
	as, _ := a["seendate"].(string)
	bs, _ := b["seendate"].(string)
	return strings.Compare(as, bs)
}
