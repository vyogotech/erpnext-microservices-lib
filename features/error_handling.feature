Feature: Comprehensive Error Handling
  As a microservice developer
  I want the framework to handle common Frappe exceptions
  So that I can return proper HTTP status codes to clients

  Scenario: Resource not found
    Given a microservice app is running
    When a request is made to an endpoint that raises "DoesNotExistError"
    Then the status code should be 404
    And the response should contain message "Test detail message"

  Scenario: Access denied
    Given a microservice app is running
    When a request is made to an endpoint that raises "PermissionError"
    Then the status code should be 403
    And the response should contain message "Test detail message"

  Scenario: Validation error
    Given a microservice app is running
    When a request is made to an endpoint that raises "ValidationError"
    Then the status code should be 400
    And the response should contain message "Invalid input data: Test detail message"
