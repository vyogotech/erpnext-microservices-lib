#!/bin/bash
# Test runner script for frappe-microservice-lib
# Usage: ./run_tests.sh [unit|integration|all]

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Frappe Microservice Library Test Runner ===${NC}\n"

# Parse arguments
TEST_TYPE=${1:-all}

# Function to run unit tests
run_unit_tests() {
    echo -e "${YELLOW}Running unit tests...${NC}"
    pytest tests/ -m "not integration" --cov-report=term-missing
}

# Function to run integration tests
run_integration_tests() {
    echo -e "${YELLOW}Running integration tests...${NC}"
    echo -e "${YELLOW}Note: Integration tests require running services${NC}"
    pytest tests/ -m integration --no-cov
}

# Function to run all tests
run_all_tests() {
    echo -e "${YELLOW}Running all tests...${NC}"
    pytest tests/ --cov-report=term-missing --cov-report=html
}

# Run tests based on argument
case $TEST_TYPE in
    unit)
        run_unit_tests
        ;;
    integration)
        run_integration_tests
        ;;
    all)
        run_all_tests
        ;;
    *)
        echo -e "${RED}Invalid argument: $TEST_TYPE${NC}"
        echo "Usage: ./run_tests.sh [unit|integration|all]"
        exit 1
        ;;
esac

# Check exit code
if [ $? -eq 0 ]; then
    echo -e "\n${GREEN}✅ Tests passed!${NC}"
    echo -e "${GREEN}Coverage report: htmlcov/index.html${NC}"
else
    echo -e "\n${RED}❌ Tests failed!${NC}"
    exit 1
fi
