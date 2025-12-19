Feature: Multi-Tenancy Isolation
  As a SaaS provider
  I want each tenant's data to be isolated
  So that users only see and modify their own data

  Scenario: Automatic filtering on list
    Given a microservice app is running
    And the current user belongs to "tenant_a"
    When the app queries all "Sales Order"
    Then the database filter should include "tenant_id" = "tenant_a"

  Scenario: Access record from another tenant
    Given a microservice app is running
    And the current user belongs to "tenant_a"
    When the app attempts to get "Sales Order" named "SO-TENANT-B" belonging to "tenant_b"
    Then a "PermissionError" should be raised
    And the status code should be 403

  Scenario: Access record from same tenant
    Given a microservice app is running
    And the current user belongs to "tenant_a"
    When the app attempts to get "Sales Order" named "SO-TENANT-A" belonging to "tenant_a"
    Then no "PermissionError" should be raised
