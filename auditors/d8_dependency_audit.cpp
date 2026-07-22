// Copyright (c) 2026 Total Coloring Toolkit contributors.
// SPDX-License-Identifier: MIT

// Independent finite auditor for the residual D = 8 dependency profiles.
//
// This program deliberately has no project or third-party dependencies.  It
// enumerates role-labelled dependency digraphs, applies the exact incidence
// filter, and audits the finite graph of legal root pivots.  Its output is a
// deterministic JSON receipt.  The computation does *not* assert that a
// dependency state has a physical edge-colouring realization, nor does it
// prove the open D = 8 extension statement; see the receipt limitations.

#include <array>
#include <bit>
#include <cstdint>
#include <deque>
#include <functional>
#include <iostream>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

constexpr std::size_t kMaxVertices = 7U;
constexpr std::size_t kMaxColumns = kMaxVertices - 1U;
constexpr unsigned kTargetBits = 3U;
constexpr unsigned kSourceBits = 7U;

constexpr std::string_view kAuditorVersion = "1.0.0";
constexpr std::string_view kSemanticsVersion = "exact-incidence-root-pivot-v1";

using VertexMask = std::uint8_t;

struct Profile {
  std::string_view id;
  std::size_t vertex_count;
  std::vector<unsigned> active_indegrees;
  std::vector<std::size_t> mobile_triple_columns;
  unsigned inert_multiplicity;
};

// Column c initially targets vertex c + 1; vertex 0 is the root.  The source
// subset for a column is role-labelled, so no isomorphism quotient is taken.
const std::vector<Profile> &builtin_profiles() {
  static const std::vector<Profile> profiles{
      {"d8-a-w5", 5U, {3U, 2U, 2U, 2U}, {0U}, 2U},
      {"d8-b-w6", 6U, {3U, 3U, 2U, 2U, 1U}, {0U, 1U}, 2U},
      {"d8-c-frozen-w5", 5U, {3U, 3U, 1U, 1U}, {0U, 1U}, 3U},
      {"d8-c-frozen-w6", 6U, {3U, 3U, 2U, 1U, 1U}, {0U, 1U}, 3U},
      {"d8-c-frozen-w7", 7U, {3U, 3U, 2U, 2U, 1U, 1U}, {0U, 1U}, 3U},
      {"d8-c-mobile-w6", 6U, {3U, 3U, 3U, 1U, 1U}, {0U, 1U, 2U}, 2U},
      {"d8-c-mobile-w7", 7U, {3U, 3U, 3U, 2U, 1U, 1U}, {0U, 1U, 2U}, 2U},
  };
  return profiles;
}

struct State {
  std::uint8_t root{};
  std::array<std::uint8_t, kMaxColumns> targets{};
  std::array<VertexMask, kMaxColumns> sources{};
};

struct Counts {
  std::uint64_t candidate_assignments{};
  std::uint64_t root_outdegree_at_least_two{};
  std::uint64_t root_reachable{};
  std::uint64_t dependency_admissible{};
  std::uint64_t incidence_admissible{};
  std::uint64_t initial_all_mobile_triples_fragile{};
  std::uint64_t pivot_resolved{};
  std::uint64_t pivot_unresolved{};
  std::map<unsigned, std::uint64_t> minimum_pivot_depth_histogram;
};

VertexMask vertex_bit(const std::size_t vertex) {
  return static_cast<VertexMask>(1U << static_cast<unsigned>(vertex));
}

unsigned population_count(const VertexMask mask) {
  return static_cast<unsigned>(std::popcount(static_cast<unsigned>(mask)));
}

VertexMask full_vertex_mask(const Profile &profile) {
  return static_cast<VertexMask>(
      (1U << static_cast<unsigned>(profile.vertex_count)) - 1U);
}

void validate_profile(const Profile &profile) {
  if (profile.vertex_count < 2U || profile.vertex_count > kMaxVertices) {
    throw std::logic_error("built-in profile has unsupported vertex count");
  }
  if (profile.active_indegrees.size() + 1U != profile.vertex_count) {
    throw std::logic_error("built-in profile must have one target per nonroot");
  }
  for (const unsigned indegree : profile.active_indegrees) {
    if (indegree > profile.vertex_count - 1U) {
      throw std::logic_error("built-in profile indegree is not loopless");
    }
  }
  for (const std::size_t column : profile.mobile_triple_columns) {
    if (column >= profile.active_indegrees.size() ||
        profile.active_indegrees[column] != 3U) {
      throw std::logic_error("mobile triple column is invalid");
    }
  }
}

// Reachability is computed in the active dependency digraph.  A source in
// sources[c] contributes the arc source -> targets[c].  When removed_target
// is nonnegative, that vertex and all of its incident arcs are suppressed.
VertexMask reachable_mask(const State &state, const Profile &profile,
                          const int removed_target = -1) {
  const auto root = static_cast<std::size_t>(state.root);
  if (removed_target >= 0 && root == static_cast<std::size_t>(removed_target)) {
    return VertexMask{0U};
  }

  VertexMask reached = vertex_bit(root);
  VertexMask previous{};
  do {
    previous = reached;
    for (std::size_t column = 0U; column < profile.active_indegrees.size();
         ++column) {
      const auto target = static_cast<std::size_t>(state.targets[column]);
      if ((removed_target >= 0 &&
           target == static_cast<std::size_t>(removed_target)) ||
          (state.sources[column] & reached) == 0U) {
        continue;
      }
      reached = static_cast<VertexMask>(reached | vertex_bit(target));
    }
  } while (reached != previous);
  return reached;
}

bool is_root_reachable(const State &state, const Profile &profile) {
  return reachable_mask(state, profile) == full_vertex_mask(profile);
}

// For a triple column with current target t, its dominator region is t plus
// the vertices that become unreachable when t is removed.  A source is an
// external entry exactly when it remains reachable without t.  Thus all three
// possible one-arc survivors work iff the external-entry count is three.
unsigned external_entry_count(const State &state, const Profile &profile,
                              const std::size_t column) {
  const auto target = static_cast<int>(state.targets[column]);
  const VertexMask outside_region = reachable_mask(state, profile, target);
  return population_count(
      static_cast<VertexMask>(state.sources[column] & outside_region));
}

bool has_robust_mobile_triple(const State &state, const Profile &profile) {
  for (const std::size_t column : profile.mobile_triple_columns) {
    if (external_entry_count(state, profile, column) == 3U) {
      return true;
    }
  }
  return false;
}

// Exact incidence records the holes in one inert column.  Active outdegree is
// 2 or 3 at the root and 1 or 2 elsewhere.  Consequently every summand below
// is 0 or 1, and the total number of deficits must equal the inert column's
// prescribed multiplicity.
bool has_exact_incidence(
    const std::array<std::uint8_t, kMaxVertices> &active_outdegree,
    const Profile &profile) {
  const unsigned root_outdegree = active_outdegree[0U];
  if (root_outdegree < 2U || root_outdegree > 3U) {
    return false;
  }

  unsigned deficit = 3U - root_outdegree;
  for (std::size_t vertex = 1U; vertex < profile.vertex_count; ++vertex) {
    const unsigned outdegree = active_outdegree[vertex];
    if (outdegree < 1U || outdegree > 2U) {
      return false;
    }
    deficit += 2U - outdegree;
  }
  return deficit == profile.inert_multiplicity;
}

// Seven vertices require seven source-mask bits.  Six columns require 18
// target bits, and the root uses three more: 3 + 6*3 + 6*7 = 63 bits.
std::uint64_t encode_state(const State &state, const Profile &profile) {
  std::uint64_t code = state.root;
  unsigned shift = kTargetBits;
  for (std::size_t column = 0U; column < profile.active_indegrees.size();
       ++column) {
    code |= static_cast<std::uint64_t>(state.targets[column]) << shift;
    shift += kTargetBits;
  }
  for (std::size_t column = 0U; column < profile.active_indegrees.size();
       ++column) {
    code |= static_cast<std::uint64_t>(state.sources[column]) << shift;
    shift += kSourceBits;
  }
  return code;
}

State decode_state(std::uint64_t code, const Profile &profile) {
  State state;
  state.root = static_cast<std::uint8_t>(code & 0x7U);
  unsigned shift = kTargetBits;
  for (std::size_t column = 0U; column < profile.active_indegrees.size();
       ++column) {
    state.targets[column] = static_cast<std::uint8_t>((code >> shift) & 0x7U);
    shift += kTargetBits;
  }
  for (std::size_t column = 0U; column < profile.active_indegrees.size();
       ++column) {
    state.sources[column] = static_cast<VertexMask>((code >> shift) & 0x7fU);
    shift += kSourceBits;
  }
  return state;
}

// A pivot is legal precisely when the old root is a source of the chosen
// column.  It reverses that root-to-target incidence and moves the root to the
// old target.  The operation is an involution.  It preserves source-set sizes,
// looplessness, exact incidence, and rooted reachability.
State pivoted_state(const State &state, const std::size_t column) {
  State result = state;
  const std::size_t old_root = state.root;
  const std::size_t new_root = state.targets[column];
  result.root = static_cast<std::uint8_t>(new_root);
  result.targets[column] = static_cast<std::uint8_t>(old_root);
  result.sources[column] = static_cast<VertexMask>(
      (state.sources[column] & static_cast<VertexMask>(~vertex_bit(old_root))) |
      vertex_bit(new_root));
  return result;
}

template <typename Visitor>
void for_each_pivot_neighbor(const State &state, const Profile &profile,
                             Visitor &&visitor) {
  const VertexMask root_bit = vertex_bit(state.root);
  for (std::size_t column = 0U; column < profile.active_indegrees.size();
       ++column) {
    if ((state.sources[column] & root_bit) != 0U) {
      visitor(pivoted_state(state, column));
    }
  }
}

// Pivot edges are undirected because every pivot is an involution.  We can
// therefore discover a whole component once, run a multi-source BFS from all
// robust states in it, and memoize the exact nearest-robust distance for every
// orientation in the component.
class PivotDistances {
public:
  explicit PivotDistances(const Profile &profile) : profile_(profile) {}

  int minimum_distance(const std::uint64_t initial_code) {
    const auto known = distance_.find(initial_code);
    if (known != distance_.end()) {
      return known->second;
    }
    classify_component(initial_code);
    return distance_.at(initial_code);
  }

private:
  void classify_component(const std::uint64_t initial_code) {
    std::vector<std::uint64_t> component;
    std::unordered_map<std::uint64_t, std::size_t> index;
    component.push_back(initial_code);
    index.emplace(initial_code, 0U);

    for (std::size_t cursor = 0U; cursor < component.size(); ++cursor) {
      const State state = decode_state(component[cursor], profile_);
      for_each_pivot_neighbor(state, profile_, [&](const State &neighbor) {
        const std::uint64_t code = encode_state(neighbor, profile_);
        const std::size_t next_index = component.size();
        const auto inserted = index.emplace(code, next_index);
        if (inserted.second) {
          component.push_back(code);
        }
      });
    }

    std::vector<int> local_distance(component.size(), -1);
    std::deque<std::size_t> queue;
    for (std::size_t i = 0U; i < component.size(); ++i) {
      if (has_robust_mobile_triple(decode_state(component[i], profile_),
                                   profile_)) {
        local_distance[i] = 0;
        queue.push_back(i);
      }
    }

    while (!queue.empty()) {
      const std::size_t current = queue.front();
      queue.pop_front();
      const State state = decode_state(component[current], profile_);
      for_each_pivot_neighbor(state, profile_, [&](const State &neighbor) {
        const std::uint64_t code = encode_state(neighbor, profile_);
        const auto found = index.find(code);
        if (found == index.end()) {
          throw std::logic_error("pivot component is not closed");
        }
        const std::size_t next = found->second;
        if (local_distance[next] < 0) {
          local_distance[next] = local_distance[current] + 1;
          queue.push_back(next);
        }
      });
    }

    for (std::size_t i = 0U; i < component.size(); ++i) {
      distance_.emplace(component[i], local_distance[i]);
    }
  }

  const Profile &profile_;
  std::unordered_map<std::uint64_t, int> distance_;
};

class ProfileAuditor {
public:
  explicit ProfileAuditor(const Profile &profile)
      : profile_(profile), pivot_distances_(profile) {
    validate_profile(profile_);
    state_.root = 0U;
    source_options_.resize(profile_.active_indegrees.size());
    for (std::size_t column = 0U; column < profile_.active_indegrees.size();
         ++column) {
      const std::size_t target = column + 1U;
      state_.targets[column] = static_cast<std::uint8_t>(target);
      source_options_[column] =
          masks_of_size_excluding(target, profile_.active_indegrees[column]);
    }
  }

  Counts run() {
    enumerate_column(0U);

    // Depth zero counts all exact states that already expose a robust mobile
    // triple.  Positive depths count only initially-all-fragile states.
    counts_.minimum_pivot_depth_histogram[0U] =
        counts_.incidence_admissible -
        counts_.initial_all_mobile_triples_fragile;

    for (const std::uint64_t code : initially_fragile_) {
      const int distance = pivot_distances_.minimum_distance(code);
      if (distance < 0) {
        ++counts_.pivot_unresolved;
      } else {
        ++counts_.pivot_resolved;
        ++counts_
              .minimum_pivot_depth_histogram[static_cast<unsigned>(distance)];
      }
    }
    return counts_;
  }

private:
  std::vector<VertexMask>
  masks_of_size_excluding(const std::size_t excluded_vertex,
                          const unsigned size) const {
    std::vector<VertexMask> masks;
    const unsigned limit = 1U << static_cast<unsigned>(profile_.vertex_count);
    const unsigned excluded_bit = 1U << static_cast<unsigned>(excluded_vertex);
    for (unsigned value = 0U; value < limit; ++value) {
      if ((value & excluded_bit) == 0U &&
          static_cast<unsigned>(std::popcount(value)) == size) {
        masks.push_back(static_cast<VertexMask>(value));
      }
    }
    return masks;
  }

  void adjust_outdegrees(const VertexMask sources, const bool add) {
    for (std::size_t vertex = 0U; vertex < profile_.vertex_count; ++vertex) {
      if ((sources & vertex_bit(vertex)) == 0U) {
        continue;
      }
      const unsigned old_value = active_outdegree_[vertex];
      active_outdegree_[vertex] =
          static_cast<std::uint8_t>(add ? old_value + 1U : old_value - 1U);
    }
  }

  void enumerate_column(const std::size_t column) {
    if (column == profile_.active_indegrees.size()) {
      audit_candidate();
      return;
    }
    for (const VertexMask sources : source_options_[column]) {
      state_.sources[column] = sources;
      adjust_outdegrees(sources, true);
      enumerate_column(column + 1U);
      adjust_outdegrees(sources, false);
    }
  }

  void audit_candidate() {
    ++counts_.candidate_assignments;
    const bool root_outdegree_ok = active_outdegree_[0U] >= 2U;
    if (root_outdegree_ok) {
      ++counts_.root_outdegree_at_least_two;
    }

    const bool reachable = is_root_reachable(state_, profile_);
    if (reachable) {
      ++counts_.root_reachable;
    }
    if (!(root_outdegree_ok && reachable)) {
      return;
    }
    ++counts_.dependency_admissible;

    if (!has_exact_incidence(active_outdegree_, profile_)) {
      return;
    }
    ++counts_.incidence_admissible;

    if (!has_robust_mobile_triple(state_, profile_)) {
      ++counts_.initial_all_mobile_triples_fragile;
      initially_fragile_.push_back(encode_state(state_, profile_));
    }
  }

  const Profile &profile_;
  PivotDistances pivot_distances_;
  State state_{};
  std::array<std::uint8_t, kMaxVertices> active_outdegree_{};
  std::vector<std::vector<VertexMask>> source_options_;
  Counts counts_;
  std::vector<std::uint64_t> initially_fragile_;
};

void write_json_string(std::ostream &output, const std::string_view value) {
  static constexpr char hexadecimal[] = "0123456789abcdef";
  output.put('"');
  for (const char character : value) {
    const auto byte = static_cast<unsigned char>(character);
    switch (byte) {
    case '"':
      output << "\\\"";
      break;
    case '\\':
      output << "\\\\";
      break;
    case '\b':
      output << "\\b";
      break;
    case '\f':
      output << "\\f";
      break;
    case '\n':
      output << "\\n";
      break;
    case '\r':
      output << "\\r";
      break;
    case '\t':
      output << "\\t";
      break;
    default:
      if (byte < 0x20U) {
        output << "\\u00" << hexadecimal[(byte >> 4U) & 0x0fU]
               << hexadecimal[byte & 0x0fU];
      } else {
        output.put(static_cast<char>(byte));
      }
      break;
    }
  }
  output.put('"');
}

template <typename Value>
void write_unsigned_array(std::ostream &output,
                          const std::vector<Value> &values) {
  output.put('[');
  for (std::size_t i = 0U; i < values.size(); ++i) {
    if (i != 0U) {
      output.put(',');
    }
    output << values[i];
  }
  output.put(']');
}

void write_profile_descriptor(std::ostream &output, const Profile &profile) {
  output << "{\"profile_id\":";
  write_json_string(output, profile.id);
  output << ",\"vertex_count\":" << profile.vertex_count
         << ",\"active_indegrees\":";
  write_unsigned_array(output, profile.active_indegrees);
  output << ",\"mobile_triple_columns\":";
  write_unsigned_array(output, profile.mobile_triple_columns);
  output << ",\"inert_multiplicity\":" << profile.inert_multiplicity;
}

void write_counts(std::ostream &output, const Counts &counts) {
  output << "\"counts\":{\"candidate_assignments\":"
         << counts.candidate_assignments << ",\"root_outdegree_at_least_two\":"
         << counts.root_outdegree_at_least_two
         << ",\"root_reachable\":" << counts.root_reachable
         << ",\"dependency_admissible\":" << counts.dependency_admissible
         << ",\"incidence_admissible\":" << counts.incidence_admissible
         << ",\"initial_all_mobile_triples_fragile\":"
         << counts.initial_all_mobile_triples_fragile
         << ",\"pivot_resolved\":" << counts.pivot_resolved
         << ",\"pivot_unresolved\":" << counts.pivot_unresolved
         << ",\"minimum_pivot_depth_histogram\":{";

  bool first = true;
  for (const auto &[depth, count] : counts.minimum_pivot_depth_histogram) {
    if (!first) {
      output.put(',');
    }
    first = false;
    write_json_string(output, std::to_string(depth));
    output.put(':');
    output << count;
  }
  output << "}}";
}

void write_audit_receipt(std::ostream &output,
                         const std::vector<const Profile *> &selected) {
  // Finish every finite audit before emitting the first byte.  A computation
  // failure therefore cannot leave a partial success receipt on stdout.
  std::vector<Counts> results;
  results.reserve(selected.size());
  for (const Profile *const profile : selected) {
    results.push_back(ProfileAuditor(*profile).run());
  }

  output << "{\"kind\":\"d8_dependency_pivot_audit\","
            "\"schema_version\":1,\"auditor_version\":";
  write_json_string(output, kAuditorVersion);
  output << ",\"semantics_version\":";
  write_json_string(output, kSemanticsVersion);
  output << ",\"complete\":true,\"limitations\":[";
  write_json_string(output,
                    "Enumerates role-labelled loopless dependency digraphs; no "
                    "isomorphism quotient is taken.");
  output.put(',');
  write_json_string(
      output, "Exact-incidence and root-pivot checks do not establish physical "
              "edge-colouring realizability.");
  output.put(',');
  write_json_string(
      output,
      "Does not verify alternating-component pairing, J-rainbow safety, "
      "criticality, or the open D=8 extension statement.");
  output << "],\"profiles\":[";

  for (std::size_t i = 0U; i < selected.size(); ++i) {
    if (i != 0U) {
      output.put(',');
    }
    const Profile &profile = *selected[i];
    write_profile_descriptor(output, profile);
    output.put(',');
    write_counts(output, results[i]);
    output.put('}');
  }
  output << "]}\n";
}

void write_profile_list(std::ostream &output) {
  output << "{\"kind\":\"d8_dependency_pivot_profile_list\","
            "\"schema_version\":1,\"auditor_version\":";
  write_json_string(output, kAuditorVersion);
  output << ",\"profiles\":[";
  const auto &profiles = builtin_profiles();
  for (std::size_t i = 0U; i < profiles.size(); ++i) {
    if (i != 0U) {
      output.put(',');
    }
    write_profile_descriptor(output, profiles[i]);
    output.put('}');
  }
  output << "]}\n";
}

void write_help(std::ostream &output) {
  output << "{\"kind\":\"d8_dependency_pivot_audit_help\","
            "\"schema_version\":1,\"auditor_version\":";
  write_json_string(output, kAuditorVersion);
  output << ",\"usage\":";
  write_json_string(
      output, "d8_dependency_audit (--suite | --profile PROFILE_ID [--profile "
              "PROFILE_ID ...] | --list-profiles | --help)");
  output << ",\"options\":[";
  write_json_string(output, "--suite: audit every built-in profile");
  output.put(',');
  write_json_string(output,
                    "--profile PROFILE_ID: audit one profile; repeatable");
  output.put(',');
  write_json_string(output, "--list-profiles: list built-in profiles");
  output.put(',');
  write_json_string(output, "--help: show this help receipt");
  output << "]}\n";
}

void write_error(std::ostream &output, const std::string_view message) {
  output << "{\"kind\":\"d8_dependency_pivot_audit_error\","
            "\"schema_version\":1,\"error\":";
  write_json_string(output, message);
  output << "}\n";
}

enum class Mode { audit_suite, audit_profiles, list_profiles, help };

struct CommandLine {
  Mode mode;
  std::set<std::string, std::less<>> requested_profiles;
};

CommandLine parse_command_line(const int argument_count,
                               const char *const arguments[]) {
  bool suite = false;
  bool list = false;
  bool help = false;
  std::set<std::string, std::less<>> requested;

  for (int index = 1; index < argument_count; ++index) {
    const std::string_view argument(arguments[index]);
    if (argument == "--suite") {
      suite = true;
    } else if (argument == "--list-profiles") {
      list = true;
    } else if (argument == "--help") {
      help = true;
    } else if (argument == "--profile") {
      if (index + 1 >= argument_count) {
        throw std::invalid_argument("--profile requires a profile ID");
      }
      ++index;
      const std::string id(arguments[index]);
      if (id.empty() || !requested.emplace(id).second) {
        throw std::invalid_argument("profile IDs must be nonempty and unique");
      }
    } else if (argument.starts_with("--profile=")) {
      const std::string id(
          argument.substr(std::string_view("--profile=").size()));
      if (id.empty() || !requested.emplace(id).second) {
        throw std::invalid_argument("profile IDs must be nonempty and unique");
      }
    } else {
      throw std::invalid_argument("unknown option: " + std::string(argument));
    }
  }

  const unsigned modes =
      static_cast<unsigned>(suite) + static_cast<unsigned>(list) +
      static_cast<unsigned>(help) + static_cast<unsigned>(!requested.empty());
  if (modes != 1U) {
    throw std::invalid_argument(
        "choose exactly one of --suite, --profile, --list-profiles, or --help");
  }
  if (suite) {
    return {Mode::audit_suite, {}};
  }
  if (list) {
    return {Mode::list_profiles, {}};
  }
  if (help) {
    return {Mode::help, {}};
  }
  return {Mode::audit_profiles, std::move(requested)};
}

std::vector<const Profile *> selected_profiles(const CommandLine &command) {
  std::vector<const Profile *> selected;
  const auto &profiles = builtin_profiles();
  if (command.mode == Mode::audit_suite) {
    for (const Profile &profile : profiles) {
      selected.push_back(&profile);
    }
    return selected;
  }

  for (const Profile &profile : profiles) {
    if (command.requested_profiles.contains(profile.id)) {
      selected.push_back(&profile);
    }
  }
  if (selected.size() != command.requested_profiles.size()) {
    for (const std::string &id : command.requested_profiles) {
      bool found = false;
      for (const Profile &profile : profiles) {
        if (profile.id == id) {
          found = true;
          break;
        }
      }
      if (!found) {
        throw std::invalid_argument("unknown profile ID: " + id);
      }
    }
  }
  return selected;
}

} // namespace

int main(const int argument_count, const char *const arguments[]) {
  try {
    const CommandLine command = parse_command_line(argument_count, arguments);
    switch (command.mode) {
    case Mode::help:
      write_help(std::cout);
      break;
    case Mode::list_profiles:
      write_profile_list(std::cout);
      break;
    case Mode::audit_suite:
    case Mode::audit_profiles:
      write_audit_receipt(std::cout, selected_profiles(command));
      break;
    }
    return 0;
  } catch (const std::exception &error) {
    write_error(std::cerr, error.what());
    return 2;
  }
}
