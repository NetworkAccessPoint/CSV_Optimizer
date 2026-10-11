import os
import tempfile
import unittest

from csvopt import rules
from csvopt.table import Table

# A log shaped like a firewall export: the two directions of a managed device.
LOG = (
    "timestamp,src_ip,dst_ip,protocol,src_port,dst_port,action\n"
    "2026-03-01 00:00:00,70.222.12.5,105.181.150.6,UDP,41000,161,allow\n"
    "2026-03-01 00:00:01,70.222.12.63,105.181.150.6,UDP,41001,161,allow\n"   # /26 ends at .63
    "2026-03-01 00:00:02,70.222.12.64,105.181.150.6,UDP,41002,161,allow\n"   # just outside
    "2026-03-01 00:00:03,105.181.150.6,70.222.12.62,UDP,41003,514,allow\n"
    "2026-03-01 00:00:04,70.222.12.9,105.181.150.6,TCP,41004,23,allow\n"
    "2026-03-01 00:00:05,70.222.12.9,105.181.150.6,TCP,41005,22,allow\n"
    "2026-03-01 00:00:06,70.222.12.9,105.181.150.6,ICMP,0,8,allow\n"
    "2026-03-01 00:00:07,10.0.0.1,10.0.0.2,TCP,41007,80,deny\n"
)

TABLE = (
    "Source\tDestination\tProtocol\tPort\tPurpose\n"
    "70.222.12.0/26\t105.181.150.6\tUDP\t161\tSNMP\n"
    "105.181.150.6\t70.222.12.62\tUDP\t514\tSyslog\n"
    "70.222.12.0/26\t105.181.150.6\tTCP\t22, 23\tConfig Backup\n"
    "70.222.12.0/26\t105.181.150.6\tICMP\t8\tPing\n"
    "203.0.113.0/24\t105.181.150.6\tUDP\t161\tSNMP (미사용)\n"
)


class ParseCase(unittest.TestCase):
    def test_tab_separated_with_header(self):
        header, rows = rules.parse_table(TABLE)
        self.assertEqual(header, ["Source", "Destination", "Protocol", "Port", "Purpose"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[2][3], "22, 23")

    def test_comma_separated_and_quotes(self):
        header, rows = rules.parse_table('src,port\n10.0.0.1,"22, 23"\n')
        self.assertEqual(header, ["src", "port"])
        self.assertEqual(rows, [["10.0.0.1", "22, 23"]])

    def test_headerless_table_gets_generated_names(self):
        header, rows = rules.parse_table("10.0.0.0/8\t161\n10.1.0.0/16\t514\n")
        self.assertEqual(len(header), 2)
        self.assertEqual(len(rows), 2)      # no row was eaten as a header

    def test_blank_lines_and_ragged_rows(self):
        header, rows = rules.parse_table("a\tb\tc\n\n1.1.1.1\tUDP\n\n2.2.2.2\tTCP\t161\n")
        self.assertEqual(header, ["a", "b", "c"])
        self.assertEqual([len(r) for r in rows], [3, 3])

    def test_kinds_are_guessed_per_column(self):
        header, rows = rules.parse_table(TABLE)
        kinds = [
            rules.guess_kind([r[i] for r in rows], header[i])
            for i in range(len(header))
        ]
        self.assertEqual(kinds, ["ip", "ip", "equals", "ports", "ignore"])

    def test_description_column_is_ignored_by_name(self):
        self.assertEqual(rules.guess_kind(["백업용", "관리"], "용도"), "ignore")
        self.assertEqual(rules.guess_kind(["백업용", "관리"], "service"), "equals")


class MatcherCase(unittest.TestCase):
    def test_cidr_bounds(self):
        low, high = rules.parse_networks("70.222.12.0/26")[0]
        self.assertEqual(low, rules.ipv4_to_int("70.222.12.0"))
        self.assertEqual(high, rules.ipv4_to_int("70.222.12.63"))

    def test_bare_address_is_a_single_point(self):
        self.assertEqual(rules.parse_networks("10.0.0.7"),
                         [(rules.ipv4_to_int("10.0.0.7"),) * 2])

    def test_several_networks_in_one_cell(self):
        self.assertEqual(len(rules.parse_networks("10.0.0.0/8, 192.168.0.0/16")), 2)

    def test_junk_address_is_dropped(self):
        self.assertEqual(rules.parse_networks("없음"), [])
        self.assertIsNone(rules.ipv4_to_int("300.1.1.1"))

    def test_port_lists(self):
        self.assertEqual(rules.parse_ports("22, 23"), {"22", "23"})
        self.assertEqual(rules.parse_ports("80/443"), {"80", "443"})

    def test_ip_cache_returns_the_same_answer(self):
        self.assertEqual(rules.ip_value("10.0.0.1"), rules.ipv4_to_int("10.0.0.1"))
        self.assertEqual(rules.ip_value("10.0.0.1"), rules.ipv4_to_int("10.0.0.1"))


class RunCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "fw.csv")
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(LOG)
        self.table = Table(self.path, use_cache=False)
        self.header, self.rows = rules.parse_table(TABLE)

    def tearDown(self):
        self.table.close()
        self.dir.cleanup()

    def mapping(self):
        return rules.auto_mapping(self.table, self.header, self.rows)

    def build(self):
        return rules.build_ruleset(self.table, self.rows, self.mapping(),
                                   [r[4] for r in self.rows])

    def test_auto_mapping_follows_names_and_values(self):
        names = {c.id: c.name for c in self.table.columns}
        picked = [names.get(spec["column"]) for spec in self.mapping()]
        self.assertEqual(picked, ["src_ip", "dst_ip", "protocol", "dst_port", None])

    def test_mapping_falls_back_to_the_values_when_there_is_no_header(self):
        bare = os.path.join(self.dir.name, "bare.csv")
        with open(bare, "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join(LOG.split("\n")[1:]))   # same log, header removed
        table = Table(bare, use_cache=False)
        try:
            self.assertFalse(table.dialect.has_header)
            picked = [
                spec["column"] for spec in
                rules.auto_mapping(table, self.header, self.rows)
            ]
            positions = {c.id: i for i, c in enumerate(table.columns)}
            self.assertEqual([positions.get(c) for c in picked], [1, 2, 3, 5, None])
        finally:
            table.close()

    def test_mapping_survives_unhelpful_column_names(self):
        # A log whose header says nothing and lists the destination first:
        # column order alone would read every rule backwards.
        backwards = os.path.join(self.dir.name, "backwards.csv")
        with open(backwards, "w", encoding="utf-8", newline="") as fh:
            fh.write("f1,f2,f3,f4,f5,f6,f7\n")
            for line in LOG.split("\n")[1:]:
                if not line.strip():
                    continue
                cells = line.split(",")
                fh.write(",".join([cells[0], cells[2], cells[1]] + cells[3:]) + "\n")
        table = Table(backwards, use_cache=False)
        try:
            mapping = rules.auto_mapping(table, self.header, self.rows)
            positions = {c.id: i for i, c in enumerate(table.columns)}
            picked = [positions.get(spec["column"]) for spec in mapping]
            self.assertEqual(picked, [2, 1, 3, 5, None])   # swapped back
            result = rules.run_rules(
                table, rules.build_ruleset(table, self.rows, mapping))
            self.assertEqual(result.counts, [2, 1, 2, 1, 0])
        finally:
            table.close()

    def test_a_mapping_that_already_works_is_left_alone(self):
        mapping = self.mapping()
        self.assertEqual(
            rules.repair_mapping(self.table, self.rows, mapping, {}), mapping)

    def test_direction_swap_is_not_attempted_when_nothing_can_match(self):
        # Rules nothing in the log satisfies must stay as mapped, not churn.
        header, rows = rules.parse_table(
            "Source\tDestination\tPort\n203.0.113.1\t198.51.100.1\t161\n")
        mapping = rules.auto_mapping(self.table, header, rows)
        names = {c.id: c.name for c in self.table.columns}
        self.assertEqual([names.get(s["column"]) for s in mapping],
                         ["src_ip", "dst_ip", "dst_port"])

    def test_every_rule_counts_its_own_rows(self):
        result = rules.run_rules(self.table, self.build())
        self.assertEqual(result.counts, [2, 1, 2, 1, 0])
        self.assertEqual(result.scanned, 8)
        self.assertEqual(len(result.ids), 6)

    def test_rows_outside_a_cidr_do_not_match(self):
        result = rules.run_rules(self.table, self.build())
        # 70.222.12.64 is one past the /26 and the deny row matches nothing.
        self.assertNotIn(2, list(result.ids))
        self.assertNotIn(7, list(result.ids))

    def test_rule_of_each_matched_row(self):
        result = rules.run_rules(self.table, self.build())
        self.assertEqual(result.rule_for(0), 0)
        self.assertEqual(result.rule_for(3), 1)
        self.assertEqual(result.rule_for(4), 2)
        self.assertEqual(result.rule_for(6), 3)
        self.assertEqual(result.rule_for(2), -1)

    def test_ids_for_one_rule_and_for_all(self):
        result = rules.run_rules(self.table, self.build())
        self.assertEqual(list(result.ids_for(2)), [4, 5])
        self.assertEqual(list(result.ids_for(None)), list(result.ids))
        self.assertEqual(list(result.ids_for(4)), [])

    def test_unmatched_rules_are_reported(self):
        result = rules.run_rules(self.table, self.build())
        self.assertEqual([i for i, _ in rules.iter_unmatched_rules(result)], [4])

    def test_bucket_index_agrees_with_testing_every_rule(self):
        ruleset = self.build()
        fast = rules.run_rules(self.table, ruleset)
        slow = []
        for rid, row in self.table.iter_all():
            for index, rule in enumerate(ruleset.rules):
                if rule.matches(row):
                    slow.append((rid, index))
                    break
        self.assertEqual(list(zip(fast.ids, fast.rule_of)), slow)

    def test_bucket_key_is_the_value_fewest_rules_share(self):
        # Every rule names a protocol, so filing by protocol would put them
        # all in two buckets; the ports are what tell them apart.
        pasted = ["Protocol\tPort"]
        pasted += [f"TCP\t{port}" for port in range(1000, 1060)]
        header, rows = rules.parse_table("\n".join(pasted))
        mapping = [
            {"column": self.table.columns[3].id, "kind": "equals"},
            {"column": self.table.columns[5].id, "kind": "ports"},
        ]
        ruleset = rules.build_ruleset(self.table, rows, mapping)
        self.assertEqual(len(ruleset.buckets), len(rows))
        self.assertEqual(ruleset.key_columns, [5])
        self.assertEqual(max(len(v) for v in ruleset.buckets.values()), 1)

    def test_first_rule_wins_when_two_overlap(self):
        pasted = "Source\tPort\n70.222.12.0/26\t161\n70.222.12.5\t161\n"
        header, rows = rules.parse_table(pasted)
        ruleset = rules.build_ruleset(
            self.table, rows, rules.auto_mapping(self.table, header, rows))
        result = rules.run_rules(self.table, ruleset)
        self.assertEqual(result.counts, [2, 0])   # the wider rule claims both

    def test_range_scan_matches_a_whole_scan(self):
        ruleset = self.build()
        whole = rules.run_rules(self.table, ruleset)
        head = rules.run_rules(self.table, ruleset, start_rid=0, stop_rid=4)
        tail = rules.run_rules(self.table, ruleset, start_rid=4, stop_rid=8)
        self.assertEqual(head.scanned + tail.scanned, whole.scanned)
        self.assertEqual(list(head.ids) + list(tail.ids), list(whole.ids))
        merged = rules.merge_runs(
            [(head.ids, head.rule_of, head.scanned),
             (tail.ids, tail.rule_of, tail.scanned)],
            whole.labels,
        )
        self.assertEqual(merged.counts, whole.counts)

    def test_subset_scan_honours_the_given_ids(self):
        ruleset = self.build()
        result = rules.run_rules(self.table, ruleset, ids=[4, 5, 7])
        self.assertEqual(result.scanned, 3)
        self.assertEqual(result.counts, [0, 0, 2, 0, 0])

    def test_edited_cells_are_matched_through_the_overlay(self):
        ruleset = self.build()
        protocol = self.table.columns[3].id
        self.table.set_cells([(2, protocol, "UDP")])     # row 2 still outside the /26
        self.table.set_cells([(7, self.table.columns[1].id, "70.222.12.10"),
                              (7, protocol, "ICMP"),
                              (7, self.table.columns[2].id, "105.181.150.6"),
                              (7, self.table.columns[5].id, "8")])
        result = rules.run_rules(self.table, ruleset)
        self.assertIn(7, list(result.ids))               # the edit now satisfies "Ping"
        self.assertEqual(result.rule_for(7), 3)

    def test_rules_with_no_usable_condition_are_dropped(self):
        header, rows = rules.parse_table("Purpose\nSNMP\nSyslog\n")
        ruleset = rules.build_ruleset(
            self.table, rows, [{"column": None, "kind": "ignore"}])
        self.assertTrue(ruleset.empty)
        result = rules.run_rules(self.table, ruleset)
        self.assertEqual(result.scanned, 0)
        self.assertEqual(len(result.ids), 0)

    def test_empty_cell_means_any(self):
        header, rows = rules.parse_table("Source\tPort\n\t161\n")
        mapping = [
            {"column": self.table.columns[1].id, "kind": "ip"},
            {"column": self.table.columns[5].id, "kind": "ports"},
        ]
        ruleset = rules.build_ruleset(self.table, rows, mapping)
        self.assertEqual(len(ruleset.rules[0].conditions), 1)
        self.assertEqual(rules.run_rules(self.table, ruleset).counts, [3])

    def test_short_record_cannot_match(self):
        ruleset = self.build()
        self.assertFalse(ruleset.rules[0].matches(["only", "one"]))

    def test_contains_and_regex_kinds(self):
        header, rows = rules.parse_table("Action\nde\n")
        mapping = [{"column": self.table.columns[6].id, "kind": "contains"}]
        self.assertEqual(
            rules.run_rules(self.table, rules.build_ruleset(self.table, rows, mapping)).counts,
            [1],
        )
        mapping = [{"column": self.table.columns[6].id, "kind": "regex"}]
        header, rows = rules.parse_table("Action\n^al\n")
        self.assertEqual(
            rules.run_rules(self.table, rules.build_ruleset(self.table, rows, mapping)).counts,
            [7],
        )

    def test_limit_stops_early(self):
        result = rules.run_rules(self.table, self.build(), limit=2)
        self.assertEqual(len(result.ids), 2)


# A Palo Alto traffic export, trimmed to the columns that matter here: the
# translated address and port sit right beside the real ones, and the
# application name sits beside the IP protocol.
PANOS = (
    "Receive Time,Source address,NAT Source IP,Destination address,"
    "NAT Destination IP,Application,Source Port,Destination Port,"
    "NAT Destination Port,IP Protocol,Action\n"
    "2026/03/01 00:00:00,70.222.12.5,0.0.0.0,105.181.150.6,0.0.0.0,"
    "snmp,41000,161,0,udp,allow\n"
    "2026/03/01 00:00:01,105.181.150.6,0.0.0.0,70.222.12.62,0.0.0.0,"
    "syslog,41001,514,0,udp,allow\n"
    "2026/03/01 00:00:02,70.222.12.9,0.0.0.0,105.181.150.6,0.0.0.0,"
    "ssh,41002,22,0,tcp,allow\n"
    "2026/03/01 00:00:03,192.168.40.8,203.0.113.7,105.181.156.9,0.0.0.0,"
    "web-browsing,41003,443,8080,tcp,allow\n"
)

PANOS_POLICY = (
    "Source\tDestination\tProtocol\tPort\tPurpose\n"
    "70.222.12.0/26\t105.181.150.6\tUDP\t161\tSNMP\n"
    "105.181.150.6\t70.222.12.62\tUDP\t514\tSyslog\n"
    "70.222.12.0/26\t105.181.150.6\tTCP\t22, 23\tConfig Backup\n"
)


class PanOsCase(unittest.TestCase):
    """A vendor export where the tempting wrong column sits next to the right one."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.header, self.rows = rules.parse_table(PANOS_POLICY)

    def tearDown(self):
        self.dir.cleanup()

    def table(self, text, name="panos.csv"):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return Table(path, use_cache=False)

    def picked(self, table):
        names = {c.id: c.name for c in table.columns}
        return [
            names.get(spec["column"])
            for spec in rules.auto_mapping(table, self.header, self.rows)
        ]

    def test_translated_columns_do_not_steal_the_mapping(self):
        table = self.table(PANOS)
        try:
            self.assertEqual(
                self.picked(table),
                ["Source address", "Destination address", "IP Protocol",
                 "Destination Port", None],
            )
        finally:
            table.close()

    def test_translated_columns_listed_first_still_lose(self):
        swapped = []
        for line in PANOS.strip().split("\n"):
            cells = line.split(",")
            # NAT Source IP <-> Source address, NAT Destination IP <-> Destination
            cells[1], cells[2] = cells[2], cells[1]
            cells[3], cells[4] = cells[4], cells[3]
            cells[7], cells[8] = cells[8], cells[7]
            swapped.append(",".join(cells))
        table = self.table("\n".join(swapped) + "\n", "nat-first.csv")
        try:
            self.assertEqual(
                self.picked(table),
                ["Source address", "Destination address", "IP Protocol",
                 "Destination Port", None],
            )
        finally:
            table.close()

    def test_application_does_not_win_over_ip_protocol(self):
        table = self.table(PANOS)
        try:
            self.assertEqual(self.picked(table)[2], "IP Protocol")
        finally:
            table.close()

    def test_lowercase_protocol_still_matches_an_uppercase_rule(self):
        table = self.table(PANOS)
        try:
            mapping = rules.auto_mapping(table, self.header, self.rows)
            result = rules.run_rules(
                table, rules.build_ruleset(table, self.rows, mapping))
            self.assertEqual(result.counts, [1, 1, 1])
        finally:
            table.close()

    def test_values_alone_carry_an_export_with_no_header(self):
        table = self.table("\n".join(PANOS.strip().split("\n")[1:]) + "\n", "bare.csv")
        try:
            positions = {c.id: i for i, c in enumerate(table.columns)}
            mapping = rules.auto_mapping(table, self.header, self.rows)
            self.assertEqual(
                [positions.get(spec["column"]) for spec in mapping],
                [1, 3, 9, 7, None],
            )
            result = rules.run_rules(
                table, rules.build_ruleset(table, self.rows, mapping))
            self.assertEqual(result.counts, [1, 1, 1])
        finally:
            table.close()


if __name__ == "__main__":
    unittest.main()
