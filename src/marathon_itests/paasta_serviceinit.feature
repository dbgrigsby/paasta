Feature: paasta_serviceinit can control marathon tasks

  Scenario: paasta_serviceinit can run status
    Given a working marathon instance
    When we run the job test-service.main
    And we wait for it to be deployed
    Then paasta_serviceinit status should try to exit 0
