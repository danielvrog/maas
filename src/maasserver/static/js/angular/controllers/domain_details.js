/* Copyright 2016 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * MAAS Domain Details Controller
 */

/* @ngInject */
function DomainDetailsController(
  $scope,
  $rootScope,
  $routeParams,
  $location,
  DomainsManager,
  UsersManager,
  ManagerHelperService,
  ErrorService,
  GeneralManager
) {
  // Set title and page.
  $rootScope.title = "Loading...";

  // Note: this value must match the top-level tab, in order for
  // highlighting to occur properly.
  $rootScope.page = "domains";

  // Set flag for RSD navigation item.
  if (!$rootScope.showRSDLink) {
    GeneralManager.getNavigationOptions().then(
      res => ($rootScope.showRSDLink = res.rsd)
    );
  }

  // Initial values.
  $scope.loaded = false;
  $scope.domain = null;
  $scope.editSummary = false;
  $scope.predicate = "name";
  $scope.reverse = false;
  $scope.action = null;
  $scope.editRow = null;
  $scope.deleteRow = null;

  $scope.domainsManager = DomainsManager;
  $scope.newObject = {};

  $scope.supportedRecordTypes = [
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "NS",
    "SRV",
    "SSHFP",
    "TXT"
  ];

  // Set default predicate to name.
  $scope.predicate = "name";

  // Sorts the table by predicate.
  $scope.sortTable = function(predicate) {
    $scope.predicate = predicate;
    $scope.reverse = !$scope.reverse;
  };

  $scope.enterEditSummary = function() {
    $scope.editSummary = true;
  };

  // Called when the "cancel" button is clicked in the domain summary.
  $scope.exitEditSummary = function() {
    $scope.editSummary = false;
  };

  $scope.isRecordAutogenerated = function(row) {
    // We can't edit records that don't have a dnsresource_id.
    // (If the row doesn't have one, it has probably been automatically
    // generated by means of a deployed node, or some other reason.)
    return !row.dnsresource_id;
  };

  $scope.editRecord = function(row) {
    $scope.editRow = row;
    // We'll need the original values to determine if an update is
    // required.
    row.previous_rrdata = row.rrdata;
    row.previous_rrtype = row.rrtype;
    row.previous_name = row.name;
    row.previous_ttl = row.ttl;
    $scope.deleteRow = null;
  };

  $scope.removeRecord = function(row) {
    $scope.deleteRow = row;
    $scope.editRow = null;
  };

  $scope.confirmRemoveRecord = function(row) {
    // The websocket handler needs the domain ID, so add it in.
    row.domain = $scope.domain.id;
    $scope.domainsManager.deleteDNSRecord(row);
    $scope.stopEditingRow();
  };

  $scope.stopEditingRow = function() {
    $scope.editRow = null;
    $scope.deleteRow = null;
  };

  // Updates the page title.
  function updateTitle() {
    $rootScope.title = $scope.domain.displayname;
  }

  // Called when the domain has been loaded.
  function domainLoaded(domain) {
    $scope.domain = domain;
    $scope.loaded = true;

    updateTitle();
  }

  // Return true if the authenticated user is super user.
  $scope.isSuperUser = function() {
    return UsersManager.isSuperUser();
  };

  // Return true if this is the default domain.
  $scope.isDefaultDomain = function() {
    if (angular.isObject($scope.domain)) {
      return $scope.domain.id === 0;
    }
    return false;
  };

  // Called to check if the space can be deleted.
  $scope.canBeDeleted = function() {
    if (angular.isObject($scope.domain)) {
      return $scope.domain.rrsets.length === 0;
    }
    return false;
  };

  // Called when the delete domain button is pressed.
  $scope.deleteButton = function() {
    $scope.error = null;
    $scope.actionInProgress = true;
    $scope.action = "delete";
  };

  // Called when the add record button is pressed.
  $scope.addRecordButton = function() {
    $scope.error = null;
    $scope.actionInProgress = true;
    $scope.action = "add_record";
  };

  // Called when the cancel delete domain button is pressed.
  $scope.cancelAction = function() {
    $scope.actionInProgress = false;
  };

  // Called when the confirm delete domain button is pressed.
  $scope.deleteConfirmButton = function() {
    DomainsManager.deleteDomain($scope.domain).then(
      function() {
        $scope.actionInProgress = false;
        $location.path("/domains");
      },
      function(error) {
        $scope.error = ManagerHelperService.parseValidationError(error);
      }
    );
  };

  // Load all the required managers.
  ManagerHelperService.loadManagers($scope, [
    DomainsManager,
    UsersManager
  ]).then(function() {
    // Possibly redirected from another controller that already had
    // this domain set to active. Only call setActiveItem if not
    // already the activeItem.
    var activeDomain = DomainsManager.getActiveItem();
    var requestedDomain = parseInt($routeParams.domain_id, 10);
    if (isNaN(requestedDomain)) {
      ErrorService.raiseError("Invalid domain identifier.");
    } else if (
      angular.isObject(activeDomain) &&
      activeDomain.id === requestedDomain
    ) {
      domainLoaded(activeDomain);
    } else {
      DomainsManager.setActiveItem(requestedDomain).then(
        function(domain) {
          domainLoaded(domain);
        },
        function(error) {
          ErrorService.raiseError(error);
        }
      );
    }
  });
}

export default DomainDetailsController;
