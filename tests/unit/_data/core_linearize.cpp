// Run Bitcoin Core's Linearize and PostLinearize on clusters read from stdin.
//
// Input, one case per line: n, then n triples `fee size parents` (parents a
// bitmask of earlier positions), then max_cost, rng_seed, `old`, and k with
// k positions of a topological order. The order is Linearize's
// old_linearization where `old` is 1, and PostLinearize's input. Output, one
// line per case: Linearize's order, `optimal`, cost, then PostLinearize's
// order.
#include <cluster_linearize.h>
#include <util/bitset.h>

#include <cstdint>
#include <iostream>
#include <vector>

using namespace cluster_linearize;
using Set = BitSet<64>;

int main()
{
    int n;
    while (std::cin >> n) {
        DepGraph<Set> depgraph;
        for (int i = 0; i < n; ++i) {
            int64_t fee, size;
            uint64_t parents;
            std::cin >> fee >> size >> parents;
            auto pos = depgraph.AddTransaction(FeeFrac{fee, int32_t(size)});
            Set set;
            for (int j = 0; j < 64; ++j) if (parents >> j & 1) set.Set(j);
            depgraph.AddDependencies(set, pos);
        }
        uint64_t max_cost, seed;
        int old, k;
        std::cin >> max_cost >> seed >> old >> k;
        std::vector<DepGraphIndex> post(k);
        for (auto& p : post) std::cin >> p;
        std::vector<DepGraphIndex> old_linearization;
        if (old) old_linearization = post;
        auto [lin, optimal, cost] = Linearize(depgraph, max_cost, seed, IndexTxOrder{}, old_linearization);
        for (auto i : lin) std::cout << i << ' ';
        std::cout << "| " << optimal << ' ' << cost << " |";
        PostLinearize(depgraph, std::span{post});
        for (auto i : post) std::cout << ' ' << i;
        std::cout << '\n';
    }
}
