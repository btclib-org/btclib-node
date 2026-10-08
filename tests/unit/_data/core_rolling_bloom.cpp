// Run Bitcoin Core's CRollingBloomFilter on cases read from stdin.
//
// core_rolling_bloom.inc is Core's own MurmurHash3 and CRollingBloomFilter
// code, cut out of src/hash.cpp and src/common/bloom.cpp by the commands in
// tests/_data/README.md. Only reset() is this file's, so that the tweak is
// the input's rather than a random draw.
//
// Input, one case per line: nElements, fpRate, nTweak, then operations: `+`
// or `?` followed by a key in hex inserts or queries it, and `+n` or `?n`
// followed by a start and a count does so for each key i of the range, i as
// 32 little-endian bytes. `!` followed by a tweak resets the filter to
// it. Output, one line per case: nHashFuncs,
// nEntriesPerGeneration, data.size(), nGeneration, nEntriesThisGeneration,
// FNV-1a 64 of data's words as little-endian bytes, then one 0 or 1 per
// query.
#define private public
#include <common/bloom.h>
#undef private
#include <crypto/common.h>
#include <hash.h>
#include <util/fastrange.h>

#include <algorithm>
#include <bit>
#include <cstdint>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "core_rolling_bloom.inc"

static unsigned int g_tweak;

void CRollingBloomFilter::reset()
{
    nTweak = g_tweak;
    nEntriesThisGeneration = 0;
    nGeneration = 1;
    std::fill(data.begin(), data.end(), 0);
}

static std::vector<unsigned char> FromHex(const std::string& hex)
{
    std::vector<unsigned char> out;
    for (size_t i = 0; i + 1 < hex.size(); i += 2) {
        out.push_back(std::stoi(hex.substr(i, 2), nullptr, 16));
    }
    return out;
}

static std::vector<unsigned char> Counter(uint64_t i)
{
    std::vector<unsigned char> out(32, 0);
    for (int j = 0; j < 8; ++j) out[j] = i >> (8 * j);
    return out;
}

int main()
{
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in{line};
        unsigned int n;
        double fp;
        in >> n >> fp >> g_tweak;
        CRollingBloomFilter filter{n, fp};
        std::string answers, op;
        auto run = [&](char kind, const std::vector<unsigned char>& key) {
            if (kind == '+') {
                filter.insert(key);
            } else {
                answers += filter.contains(key) ? '1' : '0';
            }
        };
        while (in >> op) {
            if (op[0] == '!') {
                g_tweak = std::stoul(op.substr(1));
                filter.reset();
            } else if (op.size() > 1 && op[1] == 'n') {
                uint64_t start, count;
                in >> start >> count;
                for (uint64_t i = start; i < start + count; ++i) run(op[0], Counter(i));
            } else {
                run(op[0], FromHex(op.substr(1)));
            }
        }
        uint64_t fnv = 0xcbf29ce484222325;
        for (uint64_t word : filter.data) {
            for (int j = 0; j < 8; ++j) {
                fnv ^= (word >> (8 * j)) & 0xff;
                fnv *= 0x100000001b3;
            }
        }
        std::cout << filter.nHashFuncs << ' ' << filter.nEntriesPerGeneration << ' '
                  << filter.data.size() << ' ' << filter.nGeneration << ' '
                  << filter.nEntriesThisGeneration << ' ' << fnv << " | " << answers << '\n';
    }
}
